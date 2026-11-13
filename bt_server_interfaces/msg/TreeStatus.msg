# Run state and one status byte per node; also the heartbeat (design 4.2).
uint8 STATE_IDLE       = 0
uint8 STATE_RUNNING    = 1
uint8 STATE_PAUSED     = 2
uint8 STATE_STOPPING   = 3
uint8 STATE_RESTARTING = 4

uint8 NODE_READY      = 0
uint8 NODE_RUNNING    = 1
uint8 NODE_SUCCEEDED  = 2
uint8 NODE_FAILED     = 3
uint8 NODE_CANCELLING = 4

builtin_interfaces/Time stamp
unique_identifier_msgs/UUID run_id
uint64  seq
uint8   state
uint32  loop_count
uint32  cancelling                     # leaves waiting for a cancel to be confirmed
uint32  unsettled                      # goals whose cancel was never confirmed
uint8   outcome                        # of the last finished run, as in ExecuteTree; 0 while one runs or before the first
string  message
uint8[] node_status                    # one byte per node, in the order of RunInfo.node_paths
