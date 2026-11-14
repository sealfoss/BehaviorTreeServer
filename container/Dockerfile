# The Behavior Tree Server in a container: Ubuntu 26.04 and ROS 2 Lyrical, for a host that
# cannot run Lyrical itself, such as a Jetson with JetPack 6 (Ubuntu 22.04). jetson_install.sh
# builds it; jetson_run.sh and the service of jetson_setup_system_service.sh run it (see
# docs/JETSON.md). The build context is the repository root; always name the target:
#
#   docker build -f container/Dockerfile --target server -t bt-server:latest .
#   docker build -f container/Dockerfile --target test -t bt-server-test:latest .
#
# The project's own install.sh makes the image, so it holds what docs/INSTALLED.md lists, laid
# out as for the systemd services (install.sh --service): /opt/bt_server/venv, /opt/bt_server/install
# and /opt/bt_server/bin. The dependencies come first, in a layer of their own that only a change
# to install.sh or a package.xml invalidates: a change to the code rebuilds in about a minute
# instead of downloading ROS again.

ARG BASE_IMAGE=ubuntu:26.04

# ------------------------------------------------------------------------ dependencies
FROM ${BASE_IMAGE} AS deps
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 PYTHONUNBUFFERED=1
COPY install.sh /src/install.sh
COPY bt_server_interfaces/package.xml /src/bt_server_interfaces/package.xml
COPY bt_server_core/package.xml /src/bt_server_core/package.xml
COPY bt_server_executor/package.xml /src/bt_server_executor/package.xml
COPY bt_server_gateway/package.xml /src/bt_server_gateway/package.xml
COPY bt_server_bringup/package.xml /src/bt_server_bringup/package.xml
RUN /src/install.sh --prefix /opt/bt_server --service --deps-only \
 && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------------------ the server
FROM deps AS server
# The site's own apt packages, such as the interfaces of the robots' actions and services
# (ros-lyrical-nav2-msgs): jetson_install.sh passes the list in config/apt-packages. A layer of
# their own, before the code: only a change to the list installs them again.
ARG EXTRA_APT_PACKAGES=""
RUN if [ -n "$EXTRA_APT_PACKAGES" ]; then \
        apt-get update \
     && apt-get install -y --no-install-recommends $EXTRA_APT_PACKAGES \
     && rm -rf /var/lib/apt/lists/*; \
    fi
COPY . /src
RUN /src/install.sh --prefix /opt/bt_server --service --skip-apt \
 && install -m 0755 /src/run.sh /opt/bt_server/bin/run.sh \
 && install -m 0755 /src/container/entrypoint.sh /opt/bt_server/bin/entrypoint \
 && rm -rf /opt/bt_server/build /opt/bt_server/log
LABEL org.bt-server.image="server"
# The executor and the gateway, with the settings in /opt/bt_server/config (mounted); options
# after the image name go to run.sh, such as --namespace NS and --domain N.
ENTRYPOINT ["/opt/bt_server/bin/entrypoint"]

# ------------------------------------------------------------------------ the test suite
# A development install in /src, as on a development machine: the test tools, the interfaces the
# tests' scripted servers use, Playwright's Chromium and the libraries it needs. The tests run as
# an ordinary user, bttest, who owns /src, as a developer owns a checkout.
FROM deps AS test
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
# The package lists, removed above, are fetched again in the same step that uses them: install.sh
# fetches them only when python3-aiohttp has no candidate, and an installed package always has one.
# (iproute2: install.sh's development install writes settings, for which it looks up the LAN
# address with ip.)
RUN apt-get update \
 && apt-get install -y --no-install-recommends iproute2 \
 && /src/install.sh --deps-only \
 && /src/.venv/bin/python -m playwright install-deps chromium \
 && rm -rf /var/lib/apt/lists/*
COPY . /src
RUN /src/install.sh --skip-apt \
 && useradd --create-home --shell /bin/bash bttest \
 && chown -R bttest:bttest /src
LABEL org.bt-server.image="test"
USER bttest
WORKDIR /src
CMD ["bash", "-c", "source /opt/ros/lyrical/setup.bash && source install/setup.bash && exec .venv/bin/python -m pytest -q -p no:cacheprovider"]
