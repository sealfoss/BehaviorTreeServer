# Design 20.2: checked on the server

Checked on `MOTHER-BRAIN` (Ubuntu 26.04.1, ROS 2 Lyrical, rclpy from `ros-lyrical-rclpy`) on 2026-11-13.

| Assumption | Result |
|---|---|
| Ubuntu 26.04's `python3` is 3.14. | **Yes:** Python 3.14.4. |
| `rclpy.experimental.EventsExecutor` exists in Lyrical. | **Yes.** |
| An `async` action `execute_callback`, and an `async` service callback, that await an `rclpy.task.Future` work under the `EventsExecutor`. | **Yes.** The action result arrived with status SUCCEEDED after the callback awaited a Future completed by a timer; the service callback awaited a Future completed from another thread through `executor.create_task`. |
| `python3-aiohttp` is in the Ubuntu archive. | **Yes:** 3.13.3. |
| Whether Lyrical ships `AsyncNode`, and whether it has an action client. | `rclpy.experimental` has `AsyncNode`, `AsyncClient`, `AsyncService`, `AsyncPublisher`, `AsyncSubscription`, `AsyncTimer`, `AsyncClock`, but **no action client**, so the gateway spins an ordinary node on a second thread, as designed. |
| `rclpy.init(signal_handler_options=SignalHandlerOptions.NO)` leaves SIGINT and SIGTERM to the program. | **Yes:** after it, Python's own `default_int_handler` is still installed for SIGINT. |
| `~/execute` expands to the node's private name for an action server. | **Yes:** `/verify/bt_executor/execute`. |
| `Client.remove_pending_request` exists (Call Service halting, 6.5). | **Yes.** |
| `avahi-publish` is installed (package `avahi-utils`). | It was not; `avahi-utils` was installed. |
