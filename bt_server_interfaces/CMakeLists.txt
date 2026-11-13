cmake_minimum_required(VERSION 3.20)
project(bt_server_interfaces)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(unique_identifier_msgs REQUIRED)
find_package(action_msgs REQUIRED)

# The tree API of the Behavior Tree Server (design section 4, Appendix A).
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/RunInfo.msg"
  "msg/TreeStatus.msg"
  "msg/BlackboardUpdate.msg"
  "msg/NodeDetails.msg"
  "msg/LogRecord.msg"
  "msg/LogBatch.msg"
  "srv/GetSnapshot.srv"
  "srv/GetLog.srv"
  "srv/Apply.srv"
  "srv/Describe.srv"
  "srv/Validate.srv"
  "action/ExecuteTree.action"
  DEPENDENCIES builtin_interfaces unique_identifier_msgs action_msgs
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
