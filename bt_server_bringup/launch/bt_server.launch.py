"""Start an executor and the gateway in one terminal, for development (design 14.2).

    ros2 launch bt_server_bringup bt_server.launch.py \
        namespace:=cell1 params_file:=<executor.yaml> gateway_config:=<gateway.toml>

The executor is the ROS node ``/<namespace>/bt_executor``; the gateway is an ordinary process
that reads its TOML settings. Ctrl+C stops both: the executor first stops a running tree as Stop
does and waits for its cancels (design 8.2).
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    share = get_package_share_directory("bt_server_bringup")
    namespace = LaunchConfiguration("namespace")
    params_file = LaunchConfiguration("params_file")
    gateway_config = LaunchConfiguration("gateway_config")
    return LaunchDescription([
        DeclareLaunchArgument("namespace", default_value="cell1",
                              description="Namespace of the executor: /<namespace>/bt_executor"),
        DeclareLaunchArgument("params_file", default_value=os.path.join(share, "config", "executor.yaml"),
                              description="Parameter file of the executor (design 8.1)"),
        DeclareLaunchArgument("gateway_config", default_value=os.path.join(share, "config", "gateway.toml"),
                              description="Settings of the gateway (design 9.1)"),
        Node(
            package="bt_server_executor",
            executable="executor",
            name="bt_executor",
            namespace=namespace,
            parameters=[params_file],
            output="screen",
            sigterm_timeout="20",
        ),
        ExecuteProcess(
            cmd=["ros2", "run", "bt_server_gateway", "gateway", "--config", gateway_config],
            output="screen",
            sigterm_timeout="10",
        ),
    ])
