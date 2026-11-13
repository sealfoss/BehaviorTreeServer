"""The aiohttp application (design 9.1, 10.4, 13; contract 7).

| Route | |
|---|---|
| ``GET /`` | ``index.html`` of the built page, ``Cache-Control: no-cache``; "page not built" without it |
| ``GET /assets/<file>`` | the page's hashed files, cached for a year |
| ``GET /ws`` | the WebSocket (one per tab): pings every 20 s, 4 MB messages, no compression |
| ``POST /upload`` | multipart ``file`` fields (a tree, or its two files), ``dir``, ``replace``; 10 MB |
| ``GET /download/<tree key>`` | a zip of the tree's ``_BT.json`` and ``_BB.json`` |
| ``GET /download-log/<executor>/<run id>`` | the log of a run |

Every request passes the address check (``allowed_networks``) and the ``Host`` check (G-14); the
WebSocket and every request that changes something also the ``Origin`` check (G-13, G-15). No
CORS headers are sent.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from pathlib import Path

from aiohttp import WSMsgType, web

from bt_server_core.files import PathError, TreeFileError
from bt_server_core.treelog import sanitize_label

from .client import Client
from .gateway import Gateway
from .link import LinkDown, LinkFactory
from .security import address_allowed, allow, host_allowed, host_names, origin_allowed
from .settings import Settings, make_link_factory

__all__ = ["GATEWAY", "STATIC_DIR", "UPLOAD_LIMIT", "create_app"]

log = logging.getLogger("bt_server_gateway")

GATEWAY = web.AppKey("gateway", Gateway)
#: The built page (the web front end's build output).
STATIC_DIR = Path(__file__).resolve().parent / "static"
#: Largest upload (design 9.4) and largest message from a browser (design 10.2).
UPLOAD_LIMIT = 10 * 1024 * 1024
MESSAGE_LIMIT = 4 * 1024 * 1024
#: WebSocket ping interval (design 10.2).
PING_INTERVAL = 20.0
NO_CACHE = {"Cache-Control": "no-cache"}
LONG_CACHE = {"Cache-Control": "public, max-age=31536000, immutable"}
_SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))


@web.middleware
async def guard(request: web.Request, handler):  # noqa: ANN001, ANN201 - aiohttp's handler type
    """``allowed_networks``, ``Host`` and ``Origin`` checks (design 13)."""
    settings = request.app[GATEWAY].settings
    if not address_allowed(request.remote, settings.allowed_networks):
        return web.Response(status=403, text="This address may not use the Behavior Tree Server.\n")
    sockname = request.transport.get_extra_info("sockname") if request.transport is not None else None
    local = sockname[0] if isinstance(sockname, tuple) and sockname else None
    names = host_names(settings.public_names, settings.http_host, local)
    host = request.headers.get("Host")
    if not host_allowed(host, names):
        return web.Response(status=403, text="Unknown host name.\n")
    changes = request.method not in _SAFE_METHODS or request.path == "/ws"
    if changes and not origin_allowed(request.headers.get("Origin"), host, request.scheme, names):
        return web.Response(status=403, text="Requests from other sites are refused.\n")
    return await handler(request)


async def index(request: web.Request) -> web.StreamResponse:
    path = STATIC_DIR / "index.html"
    if not path.is_file():
        return web.Response(status=503, text="The page is not built: bt_server_gateway/static/index.html is missing.\n",
                            headers=NO_CACHE)
    return web.FileResponse(path, headers=NO_CACHE)


def _static_file(folder: Path, name: str) -> Path | None:
    base = folder.resolve()
    try:
        path = (base / name).resolve()
    except (OSError, ValueError):
        return None
    if base not in path.parents or not path.is_file():
        return None
    return path


async def asset(request: web.Request) -> web.StreamResponse:
    path = _static_file(STATIC_DIR / "assets", request.match_info["name"])
    if path is None:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers=LONG_CACHE)


async def static_file(request: web.Request) -> web.StreamResponse:
    """Other files at the top of the built page (an icon, for example)."""
    name = request.match_info["name"]
    path = _static_file(STATIC_DIR, name) if not name.startswith(".") else None
    if path is None:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers=NO_CACHE)


async def websocket(request: web.Request) -> web.WebSocketResponse:
    gateway = request.app[GATEWAY]
    socket = web.WebSocketResponse(heartbeat=PING_INTERVAL, max_msg_size=MESSAGE_LIMIT, compress=False)
    await socket.prepare(request)
    client = Client(gateway, socket.send_str, socket.close, request.remote or "")
    gateway.connect(client)
    try:
        async for message in socket:
            if message.type == WSMsgType.TEXT:
                try:
                    data = json.loads(message.data)
                except ValueError:
                    client.push({"t": "notice", "level": "warning", "text": "a message that is not JSON was ignored"})
                    continue
                await gateway.handle(client, data)
            elif message.type == WSMsgType.ERROR:
                break
    finally:
        await gateway.disconnect(client)
    return socket


def _json(status: int, **data: object) -> web.Response:
    return web.json_response(data, status=status)


async def upload(request: web.Request) -> web.Response:
    """A tree file, or its two files, into a folder of the trees folder (design 9.4): at most
    10 MB, checked as a tree before anything is stored."""
    gateway = request.app[GATEWAY]
    if not allow("upload", request.remote):
        return _json(403, ok=False, reason="not allowed")
    if request.content_length is not None and request.content_length > UPLOAD_LIMIT:
        return _json(413, ok=False, reason="an upload is limited to 10 MB")
    try:
        reader = await request.multipart()
    except (AssertionError, ValueError, KeyError):
        return _json(400, ok=False, reason="the upload is not multipart form data")
    files: dict[str, bytes] = {}
    folder, replace, total = "", False, 0
    while (part := await reader.next()) is not None:
        if part.name == "file":
            data = bytearray()
            while chunk := await part.read_chunk():
                total += len(chunk)
                if total > UPLOAD_LIMIT:
                    return _json(413, ok=False, reason="an upload is limited to 10 MB")
                data += chunk
            files[Path((part.filename or "tree_BT.json").replace("\\", "/")).name] = bytes(data)
        elif part.name == "dir":
            folder = (await part.text()).strip()
        elif part.name == "replace":
            replace = (await part.text()).strip().lower() in ("1", "true", "yes", "on")
    try:
        key, stored = gateway.check_upload(folder, files)
    except (ValueError, PathError) as error:
        return _json(400, ok=False, reason=str(error))
    try:
        await gateway.store_upload(key, stored, replace)
    except FileExistsError:
        return _json(409, ok=False, reason=f"the tree {key} exists", question={"kind": "replace", "name": key})
    except PermissionError as error:
        return _json(409, ok=False, reason=str(error))
    except OSError as error:
        return _json(500, ok=False, reason=f"the files cannot be stored: {error.strerror or error}")
    log.info("Upload of %r by %s", key, request.remote)
    return _json(200, ok=True, key=key)


async def download(request: web.Request) -> web.Response:
    """A zip of a tree's files, as they are on disk."""
    gateway = request.app[GATEWAY]
    try:
        files = gateway.download_files(request.match_info["key"])
    except (PathError, TreeFileError) as error:
        return web.Response(status=404, text=f"{error}\n")

    def pack() -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, path in files:
                archive.write(path, name)
        return buffer.getvalue()

    body = await asyncio.to_thread(pack)
    name = sanitize_label(request.match_info["key"].rsplit("/", 1)[-1].replace("_BT.json", "")) + ".zip"
    return web.Response(body=body, content_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', **NO_CACHE})


async def download_log(request: web.Request) -> web.Response:
    """The log of a run of an executor, read back through its link."""
    gateway = request.app[GATEWAY]
    executor, run_id = request.match_info["executor"], request.match_info["run_id"]
    if executor not in gateway.sessions and f"/{executor}" in gateway.sessions:
        executor = f"/{executor}"
    try:
        text = await gateway.log_text(executor, run_id)
    except KeyError:
        return web.Response(status=404, text=f"There is no executor named {executor}.\n")
    except LinkDown as error:
        return web.Response(status=503, text=f"The executor cannot be reached: {error}\n")
    name = f"{sanitize_label(executor.strip('/').replace('/', '_'))}_{run_id}.log"
    return web.Response(text=text, content_type="text/plain", charset="utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


def create_app(settings: Settings, factory: LinkFactory | None = None) -> web.Application:
    """The gateway's application; ``factory`` defaults to the one the settings name."""
    gateway = Gateway(settings, factory if factory is not None else make_link_factory(settings))
    app = web.Application(middlewares=[guard], client_max_size=UPLOAD_LIMIT + 64 * 1024)
    app[GATEWAY] = gateway

    async def start(app: web.Application) -> None:
        await gateway.start()

    async def shutdown(app: web.Application) -> None:
        for client in list(gateway.clients.values()):
            try:
                await client.close_socket()
            except Exception:  # noqa: BLE001
                pass

    async def cleanup(app: web.Application) -> None:
        await gateway.close()

    app.on_startup.append(start)
    app.on_shutdown.append(shutdown)
    app.on_cleanup.append(cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/ws", websocket)
    app.router.add_get("/assets/{name:.+}", asset)
    app.router.add_post("/upload", upload)
    app.router.add_get("/download/{key:.+}", download)
    app.router.add_get("/download-log/{executor:.+}/{run_id:[0-9a-f]{32}}", download_log)
    app.router.add_get("/{name}", static_file)
    return app
