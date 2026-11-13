"""bt_server_executor: the executor node of the Behavior Tree Server (design 8). It runs behaviour
trees with the engine of ``bt_server_core`` on rclpy's event loop and offers them as the tree API
of design 4 (``~/execute``, ``~/run``, ``~/status``, ...). ``run_tree`` sends a tree from a
terminal."""
