"""``python -m bt_server_gateway --config gateway.toml`` and the console script ``gateway``."""

from __future__ import annotations

import argparse
import logging
import ssl
import sys

from aiohttp import web

from .app import create_app
from .settings import SettingsError, load_settings


def main(argv: list[str] | None = None) -> int:
    """Read the settings and serve until SIGINT or SIGTERM; on the way out the blackboard and
    recovery files are written first (design 16)."""
    parser = argparse.ArgumentParser(prog="gateway", description="The Behavior Tree Server's gateway.")
    parser.add_argument("--config", required=True, help="the settings file (TOML, design 9.1)")
    parser.add_argument("--log-level", default="INFO", help="Python logging level (default INFO)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings(args.config)
    except SettingsError as error:
        print(f"gateway: {error}", file=sys.stderr)
        return 2
    context = None
    if settings.tls:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(str(settings.tls_cert), str(settings.tls_key))
    app = create_app(settings)
    scheme = "https" if context else "http"
    logging.getLogger("bt_server_gateway").info(
        "serving %s://%s:%d/ (link %s, trees in %s)", scheme, settings.http_host, settings.http_port, settings.link,
        settings.trees_dir)
    web.run_app(app, host=settings.http_host, port=settings.http_port, ssl_context=context, print=None,
                access_log=None, shutdown_timeout=5.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
