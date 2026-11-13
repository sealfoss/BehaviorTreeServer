unique_identifier_msgs/UUID run_id
uint64 seq
bool   full                            # true: every entry (at the start of a run)
string update_json                     # {"entries": [...], "removed": [...], "order": [...]}; entries
                                       # and removals are named by entry id, so a rename is an entry
                                       # with a known id and a new name; values are never shortened
