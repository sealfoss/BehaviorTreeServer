"""``python -m bt_server_executor [--ros-args ...]``: start the executor node (design 8)."""

import sys

from .node import main

sys.exit(main())
