# What is loaded: the bundle and the table of its nodes (design 4.1).
builtin_interfaces/Time stamp
unique_identifier_msgs/UUID run_id     # the goal id; all zero when nothing is loaded (sent at start-up)
string   label
string   requested_by
string   bundle_json
string[] node_paths                    # instance path of each node; index = position in node_status
