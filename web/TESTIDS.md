# `data-testid` attributes of the page

Stable hooks for the browser tests (design B.13, P-01..P-12). `<id>` is a node id (32 hex
digits), `<name>` a blackboard entry name, `<key>` a document key such as `robot` or
`cells/patrol`. Unless noted, an id is unique on the page.

## Canvas

| Test id | Element |
|---|---|
| `canvas` | The canvas (focusable; takes the keyboard for Delete, Ctrl+A, Esc, copy and paste). |
| `world` | The world layer; its `style.transform` is `translate(panX, panY) scale(zoom)`. |
| `node-<id>` | A node, note or group. Nodes carry `data-status` (`ready`, `running`, `succeeded`, `failed`, `cancelling`) and `data-type` (the node type). |
| `title-<id>` | A node's title label (drag it to move the node). |
| `status-<id>` | A node's status label; its `title` attribute is the node details tooltip. |
| `parent-<id>` / `children-<id>` | The Parent and Children (Negation: Child) labels; `data-state` is `disconnected`, `placing` or `connected`. |
| `fields-<id>` | The field editors of a leaf. |
| `field-<id>-<field>` | One field editor, by field name (`Message`, `x`, `ValueName`, `Operator`, `CompareTo`, `LiteralValue`, `Wait`, `WaitTimeout`, `Level`, `Source`, `File`, `Executor`, ...). Doubles are text boxes. |
| `rename-<id>` | The title editor shown by Rename... (Enter keeps, Esc restores). |
| `field-<id>-File`, `browse-<id>` | A Sub-Tree node's File (its `title` names the resolved tree) and its "…" button (`dialog-choose-subtree`). |
| `conn-<parent>-<child>` | A connection (SVG group: line, wide hit path, order badge). |
| `order-<parent>-<child>` | The order number of that connection (its text is the number). |
| `drag-line` | The line while a connection is dragged; class `valid` when it is green. |
| `note-text-<id>` | A note's text (click to edit); its computed `font-size` is the fitted size. |
| `note-editor-<id>` | The note's text editor while editing. |
| `resize-<edge>-<id>` | Resize handles of notes and groups; `<edge>` is `l`, `r`, `t`, `b`, `tl`, `tr`, `bl`, `br`. |
| `group-title-<id>` | A group's title bar (drag to move the group and what lies completely inside it). |
| `group-name-<id>` / `group-name-editor-<id>` | The group's name, and its editor. |
| `read-only-banner` | Shown while a run's tree (not a document of the session) is shown. |

## Menus

| Test id | Element |
|---|---|
| `context-menu` | The page's context menu. |
| `menu-<label>` | A menu item: its label with a trailing `...` and a trailing `(...)` note removed, other runs of non-alphanumeric characters replaced by `-`. Add Node items: `menu-Sequence`, `menu-Selector`, `menu-Parallel`, `menu-Negation`, `menu-Evaluation`, `menu-Set`, `menu-Restart`, `menu-Pause`, `menu-Stop`, `menu-UDP-Message`, `menu-Print-Message`, `menu-Sub-Tree`, `menu-Remote-Sub-Tree`, `menu-Call-Action`, `menu-Call-Service`, `menu-Publish`, custom leaves by title, `menu-Notes`, `menu-Group`. Node menu: `menu-Rename`, `menu-Edit-Text`, `menu-Zoom-In`, `menu-Select-Children`, `menu-Sequence`, `menu-Selector`, `menu-Parallel`, `menu-Memory`, `menu-Policy-All`, `menu-Policy-Any`, `menu-Inspector`, `menu-Delete`. Group menu: `menu-Rename`, `menu-Select-Contents`, `menu-Border-Color` (submenu `menu-Blue`, `menu-Green`, `menu-Red`, `menu-Orange`, `menu-Purple`, `menu-Teal`, `menu-Gray`, `menu-Custom`), `menu-Promote-to-Sub-Tree`. Line menu: `menu-Delete-Connection`. Disabled items carry `aria-disabled="true"`. |

## Toolbar

| Test id | Element |
|---|---|
| `toolbar` | The toolbar. |
| `btn-new`, `btn-load`, `btn-save`, `btn-save-as`, `btn-files`, `btn-revert`, `btn-go-back` | File and view buttons. |
| `btn-execute`, `btn-pause`, `btn-stop`, `btn-reset`, `btn-configure` | Run buttons (Execute resumes when paused). |
| `chk-loop` | Loop Execution. |
| `run-state` | The run state: `IDLE`, `RUNNING · loop n`, `PAUSED · loop n`, `STOPPING (n cancels outstanding)`, `RESTARTING`, `STARTING`, `OFFLINE`, `EXECUTOR DOWN`; `IDLE (succeeded)` and so on after a run. |
| `btn-view-run` | Shows a run the session did not start, read-only. |
| `btn-findings` | Opens the findings list (shown while there are findings). |
| `breadcrumb` | The trees shown (`robot › patrol`), ` *` when the shown one has unsaved changes. |
| `executor-picker` | The executor picker (a `select`). |
| `attached-count` | Browsers attached to the session (`?` while offline). |
| `connection-state` | Connection indicator; `offline` (an extra banner) while reconnecting. |

## Side panel

| Test id | Element |
|---|---|
| `tab-blackboard`, `tab-log`, `tab-inspector` | Panel tabs. |
| `blackboard` | The Blackboard tab. |
| `bb-row-<name>` | An entry's row (`data-entry-id` holds its id; class `stale` greys it). |
| `bb-select-<name>`, `bb-name-<name>`, `bb-value-<name>`, `bb-items-<name>`, `bb-reset-<name>`, `bb-source-<name>`, `bb-age-<name>`, `bb-problem-<name>`, `bb-up-<name>`, `bb-down-<name>` | The row's controls (`bb-value-` is the value editor, or the "show all" button of a shortened value; `bb-problem-` why a source cannot feed the entry). |
| `bb-select-all`, `bb-new-type`, `bb-add`, `bb-remove` | Select All, the type of a new entry, Add Entry, Remove Entries. |
| `log-pane` | The Log tab. |
| `log-line` | Each log line (not unique; `data-seq` is its number). |
| `log-level-filter`, `log-older`, `log-follow` | Level filter, fetch older lines, follow the end again. |
| `inspector`, `inspector-title`, `inspector-title-input`, `inspector-target` | The inspector of the selected node. |
| `iface-row-<path>`, `bind-<path>`, `pin-<path>`, `output-<path>` | Rows of a ROS leaf's form by field path (`Goal.pose.position.x`), its binding, pin and output editors; `bind-<field>` for other leaves. |
| `remote-entry`, `local-entry` | New Remote Sub-Tree copy-in/copy-back pair. |
| `inspector-wait-for-cancels`, `inspector-cancel-timeout` | Per-node cancel options. |

## Dialogs

| Test id | Element |
|---|---|
| `dialog-files` | Files (New, Load, Save As, Files...): `file-list`, `file-<key>`, `file-name`, `btn-file-ok`, `btn-file-close`, `btn-file-rename`, `btn-file-delete`, `btn-file-download`, `file-upload-input`, `file-upload-dir`, `btn-file-upload`. |
| `dialog-configure` | Configure: `cfg-tick`, `cfg-mode`, `cfg-log-level`, `cfg-restore`, `cfg-memory`, `cfg-wait-for-cancels`, `cfg-cancel-timeout`, `cfg-on-unconfirmed`, `btn-configure-ok`, `btn-configure-cancel`. |
| `dialog-findings` | Findings: `finding-<n>` (click to show its node), `btn-execute-anyway`, `btn-findings-close`. |
| `dialog-add-entry` | Add Entry: `add-entry-name`, `add-entry-type`, `add-entry-value`, `add-entry-error`, `btn-add-entry-ok`, `btn-add-entry-cancel`. |
| `dialog-source` | Source...: `src-topic`, `src-type`, `src-field` (with the type's field paths in `src-fields`), `src-rate`, `src-age`, `btn-src-test`, `src-test-result`, `btn-src-ok`, `btn-src-remove`, `btn-src-cancel`. |
| `dialog-entry-value` | The whole value of a shortened entry: `entry-value-text`, `btn-entry-value-save`, `btn-entry-value-close`. |
| `dialog-unsaved`, `dialog-replace`, `dialog-choose-file`, `dialog-choose-subtree`, `dialog-revert`, `dialog-recovery`, `dialog-rename-file`, `dialog-delete-file`, `dialog-question` | Questions. Inside: `dialog-input`, `dialog-choices`, and buttons `dialog-btn-<value>`: `save`, `discard`, `cancel` (unsaved); `yes`, `cancel` (replace: Save As, New, upload; revert); `use` (Open), `create` (Create New Tree), `cancel` (choose file); `yes`, `no` (recovery: Restore, Discard; closed when another tab answers); `ok`, `cancel` (others). |
| `notice` | A notice (not unique). |
