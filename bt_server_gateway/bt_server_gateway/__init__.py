"""bt_server_gateway: the web server of the Behavior Tree Server (design 9, 10, 13).

It holds the tree documents, serves the page and one WebSocket per browser tab, and drives
runs through an executor link: ``RosLink`` (the tree API over ROS) or ``LocalLink`` (the engine
inside this process). Start it with ``python -m bt_server_gateway --config gateway.toml``.
"""
