# GUV-CFD GUI audit: server (Dash) vs PC (Qt) app

Both UIs are two front ends over the same pipeline code (`guvcfd/app.py`, run via
`start_server.bat`/`python -m guvcfd.app`, vs `guvcfd/qtapp/*.py`, run via
`StartPCApp.bat`/`python -m guvcfd.qtapp`) — confirmed byte-identical between the
GUV-CFD and GUV-CFD-PC repos, so this is a single comparison, not a per-repo one.

This audit catalogs every GUI element in both apps — tabs, labels, tooltips, buttons,
dropdowns, tables, modals/dialogs, and error/info/warning messages — quotes the exact
text on each side, names the function/callback (Dash) or method/slot (Qt) behind it,
and flags where the two diverge. ~188 elements catalogued across three passes (Project
Setup, Run Simulations, Analysis of Results + app shell/menus/settings).

Legend used throughout: **MATCH** / **TEXT DIFFERS** / **BEHAVIOR DIFFERS** /
**MISSING IN SERVER** (Qt has it, Dash doesn't) / **MISSING IN PC** (Dash has it, Qt doesn't).

---

## Executive summary — ranked by materiality

### Real bugs / data-integrity risks (fix regardless of sync preference)

1. **Qt has no overwrite-confirmation dialog before a run.** Starting a run on a case
   directory that already has results silently regenerates the mesh and overwrites
   everything in place — no warning, no equivalent to Dash's `"...Continue anyway?"`
   confirm dialog. Not documented as an intentional simplification anywhere in the Qt
   source. **Real data-loss risk.** *(Run §6, item 34)*
2. **Qt's Advanced Settings dialog has no save-time validation guard** for the
   `t-infinity-early-stop-enabled` + `keep-all-timesteps` combination. Dash explicitly
   blocks this pairing with an inline error, citing a known (since-fixed, but still
   precautionarily blocked) directory-naming corruption bug. Qt saves it unconditionally.
   *(Analysis §3, item 13)*
3. **Qt's results table shows `mixing_efficiency`, `mixing_efficiency_corrected`, and
   `spatial_cov_final` as raw 0-1 fractions instead of percentages** (e.g. `0.834`
   instead of `83.4%`), and has no unit suffix on any row. These are the same JSON
   values Dash correctly scales by ×100 — a Qt user can easily misread `0.834` as
   "0.8%". Concrete display-correctness bug, not polish. *(Analysis §7, item 51)*
4. **Qt has no pre-run required-field validation.** Dash's `_validate_settings` blocks
   a run with a named-field error message if e.g. inlet width is blank; grepping the
   entire Qt package for "validate"/"required" found nothing equivalent. A run can be
   launched from Qt with blank required geometry. *(Project Setup §12)*
5. **Qt never clamps position fields to the loaded room's actual dimensions**, and
   allows much wider opening-size/position ranges than Dash (e.g. opening size up to
   20 m vs Dash's 2 m cap; fan radius up to 5 m vs 1.5 m). Dash dynamically clamps
   every position field's max to the room size once a project loads; Qt's ranges are
   static and never update. A Qt user can silently place geometry outside the room.
   *(Project Setup §4, §13)*

### Known, documented gaps (deliberate simplifications — still worth tracking)

6. **Grid-alignment conflict-resolution flow is entirely absent from Qt** (confirmed
   by exhaustive grep — zero matches for "grid_align"/"alignment" anywhere in
   `guvcfd/qtapp/`). A Qt user opening a project with misaligned opening/source
   geometry gets no notification and no fix-it dialog, unlike Dash's two-stage
   sequential + bulk modal flow. This matches the 2026-08-07 redesign plan, which
   explicitly deferred Qt support to "a later phase" — confirmed that phase hasn't
   happened yet. *(Project Setup §11)*
7. **No flow-convergence-undecided / Phase-1-extrapolation-undecided decision UX in
   Qt.** Documented in both `run_tab.py` and `run_state.py`'s own docstrings. Dash
   shows a reassuring "Not an error, not hung" panel with Continue/Accept/Stop
   options; Qt surfaces the identical condition as a hard `"Failed: ..."` error using
   the raw exception text, with no in-app path forward except rerunning (or switching
   to the Dash app). Same gap for resuming an unfinished steady-state run. *(Run §6,
   items 31-33)*

### Substantial content gaps (not documented as intentional)

8. **Qt's analysis charts are a much simplified version of Dash's.** Same source data,
   far less analytical content: no idealized reference curves on the decay plot
   (Dash overlays "ventilation-only" and "well-mixed+UV" exponential references — the
   whole point of that plot per its own docstring); the steady-state plot isn't
   normalized to %, doesn't shift phase 2 onto a continuous timeline (so the two
   phases overlap on the same x-range instead of reading left-to-right), and has no
   UV-on marker or steady-state reference lines; neither chart has a fallback message
   for trimmed sweep reports (Dash does, with actionable text). This is the single
   largest area of drift in the whole audit. *(Analysis §6)*
9. **Qt's results table omits whole categories of content** Dash shows: fluence rate,
   target T_ss, injection rate, per-phase T_ss/CV/extrapolation detail, monitoring-
   location rows, and — notably — **all of Dash's explanatory notes, including the
   mixing-uniformity warning** that flags a poorly-mixed room (a materially important
   caveat for interpreting the numbers). *(Analysis §7, items 49-50)*
10. **Stop/Pause clicks are silent in Qt** — no log-line confirmation of the click, and
    the specific stage-of-stop detail Dash's `StoppedByUser` exception carries (e.g.
    `"Stopped before pimpleFoam."`) is discarded entirely (`except StoppedByUser:
    state.status = "stopped"`). *(Run §4, items 4-6, 24)*
11. **Numerous silent default-value mismatches** between a brand-new project in Dash
    vs Qt: opening size default 0.3 (Dash) vs 0.4 (Qt); inlet/outlet Z-position
    defaults differ (2.1/0.4 vs a flat 1.5 in Qt); fan speed default 0.3 vs 0.5;
    Phase 1/2 iteration defaults 8000/3000 (Dash) vs 4000/2000 (Qt, exactly half);
    monitoring points 2/3 default to spread-out X positions in Dash (75%/25% of room
    width) vs a flat 2.0 in Qt. None of these are visually flagged to the user — two
    "default" projects created fresh in each app describe different rooms.
    *(Project Setup §13)*

### Architectural note (currently in sync, but fragile)

12. **Validation-message text is duplicated, not shared, between the two apps.**
    Sealed-room and mechanical-ACH-only error strings are currently byte-for-byte
    identical, but Qt's `helpers.py` deliberately re-implements them rather than
    importing from `app.py` ("to keep this app fully decoupled..."). No structural
    guarantee they stay in sync — a future wording/logic change on one side won't
    propagate to the other. *(Project Setup §12; Run §5, items 26-29)*

### Feature parity — each app has things the other lacks

- **Missing in server (Dash) only:** "Open Recent" MRU submenu (Qt has a full
  10-entry persisted, self-pruning list); a shell-level "New Project from .guv
  file..." menu entry (Dash's equivalent is tab-scoped only); auto-switch to the
  Analysis tab when a run finishes (Qt does this); a persistent on-screen indicator
  of which project is currently open (Qt's `project_label` at least shows the room;
  though see item below — neither shows the `.guvcfd` file name persistently).
- **Missing in PC (Qt) only, beyond the items above:** a project "Description" field
  (present in Dash, invisible/unreachable in Qt though the data survives in the JSON);
  a persistent indicator of the currently open/saved `.guvcfd` file; a "Suggest
  duration" button in the simulation-settings dialog; a "Reset to defaults" button in
  Advanced Settings; a ParaView-launch success message describing what was set up;
  the sweep table's "Est. time to finish" column and live per-combo stage label
  (Qt shows a flat "pending" for every not-yet-finished combo); elapsed/total-run-time
  display anywhere on the Run tab.

Full detail — every element, exact quoted text on both sides, source function/line,
and verdict — is in the three sections below.

---

# Part 1 — Project Setup tab

Sources read:
- Dash: `guvcfd/app.py` — helpers `_card`/`_labeled`/`_settings_field`/`_settings_checkbox_field`/`_position_field`/`_opening_controls`/`_second_opening_controls`/`_fan_position_controls`/`_injection_position_controls`/`_monitoring_point_controls` (~L1751-1896), `project_setup_tab` layout (L1899-2037), `simulation_settings_modal` (L2209-2311), `grid_align_modal`/`grid_align_seq_modal` (L2925-2951), `POSITION_FIELDS`/`SETTINGS_FIELDS`/`WALL_OPTIONS` (L60-169), validation helpers `_sealed_room_error`/`_mechanical_ach_only_error`/`_validate_settings` + required-field tables (L755-849+), callbacks clustered L2953-5286 (position-field sync L3024-3097, load/save/open project L3192-3392, `_open_project`/grid-align sequential walk L3708-3940, live preview `_update_preview` L5188-5268).
- Qt: `guvcfd/qtapp/project_setup_tab.py` (full file, 602 lines), `guvcfd/qtapp/helpers.py` (full file, 186 lines — `sealed_room_error`/`mechanical_ach_only_error` deliberately re-implemented, not imported), `guvcfd/qtapp/main_window.py` (full file, 130 lines — File/Help menu, recent-projects).
- Cross-reference: `guvcfd/run_pipeline.py` `walk_opening_alignment_conflicts` (L769+).

## 1. Project card (load/open/save, description, status)

| Element | Dash (verbatim) | Qt (verbatim) | Verdict | Note |
|---|---|---|---|---|
| Card/box title | `"Project"` (`_card("Project", ...)`, app.py L1902) | `"Project"` (`QGroupBox("Project")`, L213) | MATCH | — |
| Load button | `"Load .guv file..."` id `load-btn` → `_load_project` (app.py L1903, L3200) | `"Load .guv file..."` → `load_project_dialog`→`load_project` (L221, L503) | MATCH | — |
| Load-status text | `f"Loaded {name}: {room.x:.2f} x {room.y:.2f} x {room.z:.2f} {room.units}, {len(room.lamps)} lamp(s)"` (`_load_project`, L3216) | `f"{Path(path).name} — room {room.x:.2f}×{room.y:.2f}×{room.z:.2f} {room.units}, {len(room.lamps)} lamp(s)"` (`load_project`, L520) | TEXT DIFFERS | "Loaded X: WxHxD ..." vs "X — room W×H×D ...", different separator words/glyphs ("x" vs "×", "Loaded"/":" vs em-dash). Cosmetic but user-visible every load.
| Idle/empty state text | none — `project-status` Div starts empty, shows nothing until a load happens | `"No project loaded"` (`self.project_label`, L226) | MISSING IN SERVER | Minor UX polish; Dash gives no feedback at all before first load.
| Persistent "current project file" indicator | Top bar: `"Project file: "` + `"Untitled project"` id `project-name-display`, updated by Open/Save/Save As (`_open_outputs[0]`, `_save_project` L3359-3392, `_open_project`) | **None.** No persistent display of the currently open/saved `.guvcfd` path anywhere (window title stays `f"GUV-CFD v{APP_VERSION}"`, never updated; `project_label` only shows the referenced `.guv` room, not the `.guvcfd` settings-file name) | MISSING IN PC | Real gap: a Qt user has no persistent on-screen indication of which `.guvcfd` project (if any) is currently loaded/saved-to, only the room file. Worth syncing.
| Description field | `dcc.Textarea` id `project-description`, label `"Description"`, auto-filled on load to `f"{room.x:.2f} x {room.y:.2f} x {room.z:.2f} {room.units} room"` (`_labeled`, L1906; `_load_project` L3217) | **Absent entirely** — no description widget anywhere in `project_setup_tab.py` | MISSING IN PC | Real gap — field exists in `SETTINGS_FIELDS` and round-trips through `.guvcfd`; Qt neither shows nor preserves it (a Qt `apply_settings()` on a Dash-saved project silently has nowhere to put/show `project-description`, though it is preserved in the raw JSON since Qt's `set_value` no-ops on unknown ids — data survives, just invisible/unreachable in Qt).
| Project-card tooltip | none | `"\"Load .guv file...\" starts a brand-new GUV-CFD project from a raw room/lamp design file (no inlet/outlet/fan settings yet - room defaults only). To reopen a project you've already configured and saved (a .guvcfd file, which remembers those settings too), use File > Open Project instead."` (box tooltip, L214-218) plus a matching button tooltip (L222-223) | MISSING IN SERVER | Qt-only explanatory tooltip distinguishing "new from .guv" vs "reopen .guvcfd" — genuinely useful UX Dash lacks (no tooltip on `load-btn` at all). Worth porting to Dash.
| File menu — New from .guv | *(not in File dropdown at all — only the in-card "Load .guv file..." button, above)* | `"New Project from .guv file..."` (`main_window.py` L45, calls same `load_project_dialog`) | TEXT DIFFERS / structural | Same action, Qt exposes it a 2nd time via File menu; cosmetic/organizational only.
| File menu — Open | `"Open Project..."` id `menu-open` → `_open_project` (app.py L2994, L3713) | `"Open Project..."` (Ctrl+O) → `open_guvcfd_dialog`→`load_guvcfd_project` (L53-56) | MATCH | Both open a saved `.guvcfd`.
| File menu — Save | `"Save Project"` id `menu-save` → `_save_project` (L2995, L3365) | `"Save Project"` (Ctrl+S) → `save_project_dialog(force_new_path=False)` (L62-65) | MATCH | —
| File menu — Save As | `"Save Project As..."` id `menu-save-as` (L2996) | `"Save Project As..."` (Ctrl+Shift+S) (L66-69) | MATCH | —
| **Open Recent submenu** | **Absent.** No recent-projects list/menu anywhere in app.py (grepped "recent" — zero hits outside unrelated code comments). | `"Open Recent"` submenu (`main_window.py` L58-59), backed by `QSettings`-persisted list of up to 10 most-recent projects, deduplicated, most-recent-first (`project_setup_tab.py` L462-499); empty state shows disabled `"(no recent projects)"` item; opening a since-deleted entry shows `QMessageBox.warning("File not found", f"{path}\n\nno longer exists - removing it from the recent-projects list.")` and self-prunes | MISSING IN SERVER | Genuine convenience feature Qt has that Dash entirely lacks. Worth porting (or intentionally deferring) — flag either way.
| Settings entry point | `dbc.Button("Settings", id="menu-settings", ...)` top bar (L2999) | `"Settings..."` `QAction` on menu bar (`main_window.py` L71-73) | MATCH (Advanced Settings, out of scope for content) | Same access point conceptually.

## 2. OpenFOAM project directory

| Element | Dash | Qt | Verdict | Note |
|---|---|---|---|---|
| Card/box title | `"OpenFOAM project directory"` (lowercase p/d, `_card`, L1912) | `"OpenFOAM Project Directory"` (Title Case, `QGroupBox`, L232) | TEXT DIFFERS | Capitalization only.
| Field label | `"Project directory (WSL path)"` (`_labeled`, L1913) | **none** — bare `QLineEdit`, no label at all (L234) | MISSING IN PC | Cosmetic but real: no on-screen indication this must be a WSL path.
| Placeholder | `r"\\wsl.localhost\Ubuntu\home\...\run"` (dcc.Textarea placeholder, L1916) | **none** | MISSING IN PC | Same gap — Qt gives no example/format hint.
| Browse button | `"Browse..."` id `browse-case-dir-btn` → `_browse_case_dir` (native Tk dir chooser) (L1919, L3227) | `"Browse..."` → `_browse_case_dir` (`QFileDialog.getExistingDirectory`, title `"Choose (or create) an OpenFOAM project directory"`) (L236-244) | MATCH (behavior), TEXT DIFFERS (dialog title only shown in Qt) | Functionally equivalent.
| Auto-fill on load | `_fresh_case_dir(path)` under `$FOAM_RUN` (WSL), name-collision-avoiding (`-2`, `-3`, ...) | `helpers.fresh_case_dir(...)` — same algorithm, only if field currently empty (L522-524) | BEHAVIOR DIFFERS (minor) | Dash always overwrites `case-dir` on every fresh `.guv` load (`Output("case-dir","value",allow_duplicate=True)`, L3196/3218); Qt only fills it if the field is currently blank (`if not self.case_dir_edit.text():`, L522) — Qt preserves a user's already-typed dir across a second Load, Dash clobbers it. Real (small) behavior gap.

## 3. Simulation type / Simulation Settings dialog (ACH, Z, decay & steady-state run settings, mechanical-ACH-only)

Both apps moved this content out of the main Project Setup panel into a modal/dialog opened from the Run Simulations tab (`simulation_settings_modal` / `simulation_settings_dialog`) — architecturally equivalent placement. Compared here since the Qt version physically lives in `project_setup_tab.py` and the fields are Project-Setup-scoped.

| Element | Dash (`simulation_settings_modal`, L2209-2311) | Qt (`_build_simulation_settings_dialog`, L295-344) | Verdict | Note |
|---|---|---|---|---|
| Dialog title | `"Simulation settings"` (ModalTitle, lowercase s) | `"Simulation Settings"` (Title Case) | TEXT DIFFERS | Cosmetic.
| Sim-type control | `dbc.RadioItems` id `sim-type`, options `"Decay"` / `"Steady state"`, values `decay`/`steady_state`, default `decay` (L2215-2226) | `QComboBox` items `"Decay (one-time contamination event)"` / `"Steady-state (continuous source)"` (L247-249) | TEXT DIFFERS | Dash terse, Qt descriptive — meaning equivalent, wording very different (a user reading docs/screenshots of one app won't recognize the other's labels).
| ACH / Z editing model | **Not editable here at all** — comment (L1924-1933) confirms `ach`/`z-value` moved to Run tab's comma-separated `scenario-z-values`/`scenario-ach-values` lists; a hidden legacy `ach`/`z-value` pair is kept in sync automatically for the single-value case | Directly editable here: `ach` field, label `"Ventilation ACH (0 = sealed room, decay only)"`, `_dspin(0,100,0.5,3,3.0)`; `z-value` field, label `"UV inactivation constant Z (cm²/mJ)"`, `_dspin(0,100,0.5,3,2.0)` (L252-259, L306-307) | BEHAVIOR DIFFERS (architectural) | Real UX divergence: Dash funnels all ACH/Z entry through the Run tab's sweep-list fields; Qt exposes a plain single-value ACH/Z pair directly in this dialog with no visible sweep-list concept in Project Setup. Defaults happen to match (ACH 3.0, Z 2.0) but the editing model differs.
| Decay: suggested duration | `"Suggested duration (s)"` + `"Suggest"` button id `suggest-duration-btn`, input min 10 max 7200 step 10 default 120, help text: `"Starting value only - the actual run duration is now computed adaptively per the eACH/ACH fit-target settings (Settings menu) once the well-mixed eACH estimate is known, and overrides this."` (L2229-2238) | `"Suggested duration (s)"`, `_ispin(1,100000,120)`, **no "Suggest" button**, tooltip: `"Starting value only - the actual run duration is computed adaptively once the well-mixed eACH estimate is known, and overrides this."` (L260-262, L312) | BEHAVIOR DIFFERS + TEXT DIFFERS | Range differs (Dash 10-7200 step 10 vs Qt 1-100000 step 1); Dash's help text points to "Settings menu" fit-target settings, Qt's is generic; **Qt has no "Suggest" button at all** → MISSING IN PC for the suggest-duration convenience action.
| Decay: write interval | `"Write interval (s)"`, min 1 max 600 step 1 default 10 (L2239-2241) | `"Write interval (s)"`, `_ispin(1,10000,10)` default 10 (L263, L313) | BEHAVIOR DIFFERS | Max differs (600 vs 10000); default matches.
| Mechanical-ACH-only checkbox | `"Run mechanical ACH only (no UV)"` id `mech-ach-only` (L2242-2243) | `"Run mechanical ACH only (no UV)"` (`QCheckBox`) (L264) | MATCH (label) | Verbatim identical checkbox text.
| Mechanical-ACH-only help text | `"Skips the fluence/UV-inactivation pipeline entirely and measures just the real, CFD-delivered ventilation air-change rate - for a pure ventilation study, independent of whether the project has lamps. Needs ACH>0 (real ventilation to measure)."` (static `html.Div`, L2244-2250) | Same text, plus a trailing `"Decay mode only."` sentence Dash's version lacks (tooltip, L265-269) | TEXT DIFFERS (minor) | Qt appends an extra trailing sentence not present in Dash; otherwise identical.
| Steady-state: Phase 1/2 iterations | `"Phase 1 iterations (no UV)"` min 500 max 50000 step 500 default 8000; `"Phase 2 iterations (UV on)"` min 500 max 50000 step 500 default 3000 (L2255-2260) | `"Phase 1 iterations (no UV)"` `_ispin(1,200000,4000)`; `"Phase 2 iterations (UV on)"` `_ispin(1,200000,2000)` (L270-271, L319-320) | BEHAVIOR DIFFERS | **Defaults differ materially**: Dash Phase1=8000/Phase2=3000 vs Qt Phase1=4000/Phase2=2000 (Qt defaults are half). Ranges also differ (500-50000 step 500 vs 1-200000 step 1). Labels match verbatim.
| T_ss window fraction | `"T_ss moving-average window (fraction of samples)"`, min 0.01 max 0.9 step 0.01 default 0.15, help text present (L2261-2266) | `"T_ss moving-average window (fraction)"`, `_dspin(0.01,1.0,0.01,2,0.15)`, same help text verbatim (L272-276, L321) | TEXT DIFFERS (label only) + BEHAVIOR DIFFERS (range: max 0.9 vs 1.0) | Default (0.15) matches; help text matches verbatim; label wording trimmed in Qt ("of samples" dropped).
| DeltaT scaling section heading/explainer | `"Residence-time-scaled deltaT"` + explainer paragraph (L2268-2275) | `"Residence-time-scaled deltaT (steady-state)"` group box + shorter note deferring to the checkbox tooltip (L324-329) | TEXT DIFFERS | Same underlying content, different presentation (inline vs deferred to tooltip).
| DeltaT enable checkbox | `"Scale time steps automatically"`, tooltip about not affecting flow-field stability, default True (`_settings_checkbox_field`, L2276-2281) | `"Scale time steps automatically"`, longer merged tooltip, default True (checked) (L277-284) | MATCH (checkbox text + default) / TEXT DIFFERS (tooltip wording) | —
| DeltaT effective fraction | `"Expected ACH/eACH effectiveness"`, unit `"x"`, default 0.7 (`_settings_field`, L2282-2289) | `"Expected ACH/eACH effectiveness (x)"`, `_dspin(0.01,2.0,0.05,2,0.7)` (L285-289, L332) | TEXT DIFFERS (label — Dash shows unit as a separate inline badge, Qt bakes "(x)" into the label text) | Default (0.7) matches; tooltip wording matches verbatim.
| DeltaT target fraction | `"Target fraction of steady state"`, no unit, default 0.995 (L2290-2295) | same label, `_dspin(0.5,0.9999,0.005,4,0.995)` (L290-293, L333) | BEHAVIOR DIFFERS (range: Dash's `dcc.Input` is free-form `type="number"` with no declared min/max; Qt hard-clamps 0.5-0.9999) | Default matches (0.995); tooltip wording matches verbatim.
| "Everything is a project setting" reminder | `dbc.Alert`: `"These settings will be saved the next time the project saves. Everything above is a project setting, same as Z/ACH - no separate file, no ambiguity about which value was actually used for a given project's results."` (L2297-2302) | **No equivalent reminder text anywhere in the dialog.** | MISSING IN PC | Purely informational, low priority.
| Close button | `"Close"` id `simulation-settings-close-btn` | `QDialogButtonBox.Close` (native "Close") | MATCH | —

## 4. Inlet opening controls (primary)

| Element | Dash | Qt | Verdict | Note |
|---|---|---|---|---|
| Card/group title | `"Inlet"` (`_card`, L1939) | `"Inlet"` (`_build_opening_group`, L347) | MATCH | —
| Show-in-preview checkbox | `"Show in preview"`, default True, id `inlet-show` (L1838) | `"Show in preview"`, default True, id `inlet-show` (L349-350) | MATCH (text/default) | Dash's preview callback actually filters inlet traces by this flag; whether Qt's `Preview3D` honors it wasn't verified (out of read scope), but the setting is at least wired into `gather_settings()`.
| Wall dropdown | `dcc.Dropdown` id `inlet-wall`, options = raw wall names (`WALL_OPTIONS`): `xMin`,`xMax`,`frontWall`,`backWall`,`floor`,`ceiling`; default `xMin` | `QComboBox` items same 6 raw wall strings (`WALLS`), default `xMin` | MATCH | Both show raw enum-like strings, no friendlier labels either side — consistent.
| Position 1/2 labels | `"Position 1 (m)"` / `"Position 2 (m)"` (`POSITION_FIELDS`, L145-146) | `"Position 1 (m)"` / `"Position 2 (m)"` (L356-357) | MATCH | —
| Position 1 default/range | default **1.5**, slider+input range **0-10**, step 0.05 initial — **after a `.guv` loads, both slider max AND input max are reset to the room's actual Y dimension** (`_register_position_field`/`_register_opening_wall_axes`, L3024-3097) | `_dspin(0, 50, 0.05, 3, 1.5)` — **fixed 0-50 range regardless of loaded room size, never updated** | BEHAVIOR DIFFERS (real gap) | Default value matches (1.5) but **Dash clamps the field's max to the actual room dimension once a project is loaded; Qt never does — a user can type e.g. Y=40 into a 4 m-wide room with no warning or clamp.** Applies to every position field below (fan/inject/monitor too) — flagged once here.
| Position 2 default | Dash **2.1** (`inlet-z`, L146) | Qt **1.5** (same `_dspin(...,1.5)` reused for both y and z, L357) | BEHAVIOR DIFFERS | Default-value mismatch: a brand-new project's inlet ends up in a different vertical position between the two apps.
| Opening size labels | `"Opening size, W x H (m)"` (single combined label, L1843) | `"Width (m)"` / `"Height (m)"` (two separate labeled rows, L360-361) | TEXT DIFFERS (layout) | Same two values, presented as one combined row (Dash) vs two separate rows (Qt).
| Opening size default/range | width & height: default **0.3**, min **0.05**, max **2.0**, step 0.05 (L1844-1847) | width & height: default **0.4**, min **0.01**, max **20**, step 0.05 (L358-359) | BEHAVIOR DIFFERS | Default mismatch (0.3 vs 0.4) **and** a much wider allowed range in Qt (up to 20 m vs Dash's hard cap of 2.0 m).
| Grid-snap help text | `"Position and size are automatically snapped OUTWARD to the mesh grid (cell size, Settings menu) - ..."` (`_GRID_SNAP_NOTE`, app.py L1831-1833) | `"...cell size, 0.1m by default) - ..."` (project_setup_tab.py L22-24) | TEXT DIFFERS (and arguably a real accuracy issue) | Dash points the user at "Settings menu" (where cell size is actually configured); Qt hardcodes "0.1m by default" — since mesh cell size is a per-project, JSON-only setting with no dedicated UI, Qt's note can be **misleading** for any project whose cell size was hand-edited away from 0.1 m.
| Diffuser type dropdown | `dcc.Dropdown`, options `"Direct jet"` (value `direct`) / `"Surface-attached (ceiling/wall diffuser)"` (value `ceiling`), default `direct` (`DIFFUSER_TYPE_OPTIONS`, L1826-1829) | `QComboBox` items literally `"direct"` / `"ceiling"` (raw values, no friendly text), default first item = `direct` (L363-364) | TEXT DIFFERS (real, user-visible) | Dash shows descriptive labels; Qt shows raw internal enum strings verbatim.
| Diffuser type help text | Explains both types, "Currently opt-in while a numerical instability...is being root-caused - see CHANGELOG." (L1854-1858) | Same content, drops the trailing "see CHANGELOG" reference, adds "(ceiling)" after "Surface-attached" (L366-369) | TEXT DIFFERS (minor) | —

## 5. 2nd Inlet opening controls

| Element | Dash | Qt | Verdict | Note |
|---|---|---|---|---|
| Placement | Nested **inside** the "Inlet" card, below primary controls, behind a divider (`_second_opening_controls`, L1939-1940) | **Separate** `QGroupBox("2nd Inlet")` (`_build_second_opening_group`, L112-115) | Structural / TEXT DIFFERS | Same feature, different information architecture — likely intentional platform difference, not a functional gap.
| Enable checkbox | `"Enable 2nd Inlet"` (L1867) | `"Enable 2nd inlet"` (L380) | TEXT DIFFERS | Capitalization only.
| Show-in-preview checkbox | Present (`inlet2-show`, reused from `_opening_controls`) | **Absent** (L373-404) | MISSING IN PC (caveat) | Caveat: Dash's own `inlet2-show` checkbox is **not** read by `_update_preview` (its Input list stops at `inlet2-enable`) nor listed in `SETTINGS_FIELDS` — a latent Dash-side dead control, not something worth porting to Qt as-is. Flag for Dash cleanup instead.
| Wall dropdown default | `ceiling` | `ceiling` | MATCH | —
| Position defaults | Position 1 (`inlet2-y`) default **2.0**; Position 2 (`inlet2-z`) default **1.5** (L149-150) | both default **1.5** (L390-391) | BEHAVIOR DIFFERS | Position-1 default mismatch.
| Opening size default/range | default 0.3, min 0.05, max 2.0 | default **0.3**, min **0.01**, max **20** (L392-393) | BEHAVIOR DIFFERS (range only) | Default matches; range still far wider in Qt.
| Diffuser type dropdown | Descriptive labels (same as primary inlet) | Raw `"direct"`/`"ceiling"` items, **and no tooltip at all** (inconsistent even with Qt's own primary-inlet field) (L397-399) | TEXT DIFFERS + MISSING IN PC (tooltip) | Same friendly-label gap as primary inlet, plus an internal Qt inconsistency (primary inlet's diffuser dropdown does get a tooltip, this one doesn't).

## 6. Outlet opening controls (primary) / 7. 2nd Outlet

Structurally identical to Inlet/2nd Inlet (same helper functions, `is_inlet=False` just skips the diffuser-type field on both sides). All findings from sections 4-5 apply symmetrically:

| Element | Dash | Qt | Verdict | Note |
|---|---|---|---|---|
| Card title | `"Outlet"` | `"Outlet"` | MATCH | —
| Wall default | `xMax` | `xMax` | MATCH | —
| Position 1 default | **1.5** | **1.5** | MATCH | —
| Position 2 default | Dash **0.4** (`outlet-z`) | Qt **1.5** | BEHAVIOR DIFFERS | Same pattern as inlet-z — meaningfully different default geometry for a brand-new project.
| 2nd Outlet enable text | `"Enable 2nd Outlet"` | `"Enable 2nd outlet"` | TEXT DIFFERS | Capitalization.
| 2nd Outlet wall default | `floor` | `floor` | MATCH | —
| 2nd Outlet position defaults | Position 1 default 2.0; Position 2 default 1.5 | both default 1.5 | BEHAVIOR DIFFERS | Same Position-1 mismatch pattern.
| Opening size / grid-snap note / show-in-preview | Same as inlet | Same as inlet | BEHAVIOR DIFFERS / MISSING IN PC | Mirrors sections 4-5.

## 8. Mixing Fan

| Element | Dash | Qt | Verdict | Note |
|---|---|---|---|---|
| Card/box title | `"Mixing fan"` (lowercase f, L1945) | `"Mixing Fan"` (Title Case, L407) | TEXT DIFFERS | Cosmetic.
| Enable checkbox | `"Enable fan"`, default False | `"Enable fan"`, default False | MATCH | —
| Speed label | `"Speed (m/s), 0.05–0.5 typical"` (L1948) | `"Speed (m/s)"` (L411) | TEXT DIFFERS | Dash includes an inline typical-range hint; Qt omits it.
| Speed default/range | default 0.3, min 0.05, max 1.5, step 0.01, slider with marks (L1949-1951) | `_dspin(0, 1.5, 0.05, 3, 0.5)` — default 0.5, min 0, max 1.5, step 0.05 (L411) | BEHAVIOR DIFFERS | Default mismatch (0.3 vs 0.5) — a new Qt project's default fan speed is 67% higher than Dash's.
| Direction control | `dbc.RadioItems`, labels `"Downward"`/`"Upward"`, values `down`/`up`, default `down`; **no tooltip** (L1952-1963) | `QComboBox` items literally `"down"`/`"up"`, default `down`, **with tooltip** `"Which way the fan pushes air along its own axis."` (L412-415) | TEXT DIFFERS (labels) + MISSING IN SERVER (tooltip) | Dash's radio labels are friendlier; Qt has a helpful tooltip Dash lacks.
| Radius label/default/range | `"Radius (m)"`, default 0.6, min 0.1, max 1.5, step 0.05 (L1964-1966) | `"Radius (m)"`, `_dspin(0.05, 5, 0.05, 3, 0.6)` (L416) | BEHAVIOR DIFFERS (range only) | Default matches; Qt allows up to 5 m vs Dash's 1.5 m cap.
| Thickness label/default/range | `"Thickness (m)"`, default 0.2, min 0.05, max 1.0, step 0.05 (L1967-1969) | `"Thickness (m)"`, `_dspin(0.02, 2, 0.02, 3, 0.2)` (L417) | BEHAVIOR DIFFERS (range only) | Default matches; Qt range wider (up to 2 m).
| Position fields | `"X position (m)"` / `"Y position (m)"` / `"Height — Z (m)"`, defaults **2.0 / 1.5 / 2.2**, dynamically clamped to room dims | `"X position (m)"` / `"Y position (m)"` / `"Z position (m)"`, defaults **2.0 / 1.5 / 2.2**, fixed 0-50 (never clamped) | TEXT DIFFERS (Z label) — defaults MATCH | Rare case where all three defaults line up exactly; only the Z label wording differs, and the room-clamping gap from section 4 applies here too.

## 9. Contaminant Source Geometry (steady-state)

| Element | Dash | Qt | Verdict | Note |
|---|---|---|---|---|
| Card/box title | `"Contaminant source geometry"` (L1994) | `"Contaminant Source Geometry (steady-state mode)"` (L424) | TEXT DIFFERS | Qt adds a parenthetical mode clarifier.
| Grid-snap note | Same `_GRID_SNAP_NOTE`, same "0.1m by default" difference as section 4 | Same | TEXT DIFFERS | Carries the section-4 finding into this card.
| Position labels/defaults | `"X position (m)"` / `"Y position (m)"` / `"Height — Z (m)"`, default 2.0 / 1.5 / 1.5 | `"X position (m)"` / `"Y position (m)"` / `"Z position (m)"`, defaults 2.0 / 1.5 / 1.5 | TEXT DIFFERS (Z label only) — defaults MATCH | Same pattern as fan.
| Source zone size label | `"Source zone size (m)"`, help text present, default 0.3, min 0.05, max 2.0, step 0.05 (L1997-2003) | Same label/help text verbatim, `_dspin(0.01, 5, 0.05, 3, 0.3)` (L434-438) | BEHAVIOR DIFFERS (range only) | Default matches; Qt allows up to 5 m vs Dash's 2.0 m cap.

## 10. Monitoring Points

| Element | Dash | Qt | Verdict | Note |
|---|---|---|---|---|
| Card/box title | `"Monitoring locations"` (L1974) | `"Monitoring Points (optional)"` (L443) | TEXT DIFFERS | "locations" vs "Points"; Qt adds "(optional)".
| Master enable checkbox | `"Enable monitoring locations"`, default False | `"Enable monitoring points"`, default False | TEXT DIFFERS | Consistent with the title-wording drift above.
| Per-point enable checkbox | `"Enable Point {i}"` (L1884) | `"Point {i} enabled"` (L449) | TEXT DIFFERS | Different phrasing/word order.
| Name field | `"Name"`, default `f"Point {i}"` | `"Name"`, default `f"Point {i}"` | MATCH | —
| Position labels | `"X position (m)"` / `"Y position (m)"` / `"Height — Z (m)"` | `"X (m)"` / `"Y (m)"` / `"Z (m)"` (shorter) | TEXT DIFFERS | Qt drops "position"/"Height —" entirely.
| Position defaults, Point 1 | X 2.0 / Y 1.5 / Z 1.5 | X 2.0 / Y 1.5 / Z 1.5 | MATCH | —
| Position defaults, Point 2 | X **3.0** (`0.75 × room.x`) / Y 1.5 / Z 1.5 | X **2.0** / Y 1.5 / Z 1.5 | BEHAVIOR DIFFERS | Dash spreads default point positions across the room; Qt uses the same flat 2.0 default for every point's X.
| Position defaults, Point 3 | X **1.0** (`0.25 × room.x`) / Y 1.5 / Z 1.5 | X **2.0** / Y 1.5 / Z 1.5 | BEHAVIOR DIFFERS | Same issue for Point 3.
| Cells-per-side label | `"Averaging box size (cells per side)"`, help text mentions "default cell size 0.1m", default 4, min 1, max 20, step 1 (L1890-1894) | `"Cells per side"`, shorter tooltip, `_ispin(1, 20, 4)` (L455-458) | TEXT DIFFERS (label + help text wording) — numeric range/default MATCH | Same "0.1m by default" cell-size framing issue as section 4's grid-snap note.

## 11. Grid-alignment conflict-resolution flow (fires on project open)

This entire feature is **absent from the Qt app.** Confirmed by exhaustive grep of `guvcfd/qtapp/` for "grid_align", "grid-align", "check_settings_grid_alignment", and "alignment" — zero matches anywhere in the Qt package.

| Element | Dash | Qt | Verdict | Note |
|---|---|---|---|---|
| Sequential per-opening conflict modal | `grid_align_seq_modal`, title `"Mesh grid alignment"`, body per-conflict (`_grid_align_seq_body`), buttons `"Keep this value"` / `"Use suggested value"`; driven by `walk_opening_alignment_conflicts` (run_pipeline.py L769+), auto-opens on `_open_project` (L3708-3798) whenever loaded opening geometry doesn't land on mesh-cell boundaries | **None.** | MISSING IN PC | **The flagged, expected-to-be-missing redesign — confirmed.** A Qt user who opens a `.guvcfd` project with misaligned opening geometry gets **no notification whatsoever** — the mesh generator still snaps outward and builds correctly, but the project's own saved numbers stay wrong with no prompt to fix them. Per the redesign plan, Qt support was explicitly deferred to "a later phase" — this audit confirms that phase hasn't happened yet.
| Bulk conflict modal (contaminant source zone only) | `grid_align_modal`, title `"Openings don't align with the mesh grid"` (stale — despite now being source-zone-only per a nearby comment, the title text is a leftover from before the sequential-per-opening split), buttons `"Keep as typed"` / `"Apply suggested fix"` | **None.** | MISSING IN PC | Same gap, scoped to the contaminant-source-zone check. Also note the Dash-side stale modal title as a minor internal Dash bug worth a one-line fix.
| Post-fix "must save" reminder | Appended to `project-status` after either modal applies a fix | n/a | MISSING IN PC | n/a — feature doesn't exist to remind about.

## 12. Field-level validation messages (sealed room / mechanical-ACH-only / required fields)

| Element | Dash | Qt | Verdict | Note |
|---|---|---|---|---|
| Sealed-room error, wrong sim-type | `"Sealed-room / ACH<=0 is only supported in Decay mode - steady-state has no sensible zero-ventilation case."` (`_sealed_room_error`, app.py L796-798) | Byte-for-byte identical (`sealed_room_error`, helpers.py L165-166) | MATCH (verbatim) | Text identical — see architecture note below.
| Sealed-room error, no fan | `"Sealed room (ACH<=0) needs the mixing fan enabled - ..."` (L799-801) | Byte-for-byte identical (helpers.py L167-169) | MATCH (verbatim) | —
| Mechanical-ACH-only error, wrong sim-type | `"Mechanical ACH only is only supported in Decay mode - ..."` (L819-821) | Byte-for-byte identical (helpers.py L179-181) | MATCH (verbatim) | —
| Mechanical-ACH-only error, ACH<=0 | `"Mechanical ACH only needs real ventilation (ACH>0) - ..."` (L822-824) | Byte-for-byte identical (helpers.py L182-184) | MATCH (verbatim) | —
| **Implementation architecture** | `_sealed_room_error`/`_mechanical_ach_only_error` defined once in app.py, called from every place that needs the check | `helpers.sealed_room_error`/`mechanical_ach_only_error` are **independent re-implementations**, explicitly NOT imported from app.py (module docstring: "deliberately reimplemented here rather than imported from guvcfd.app, to keep this app fully decoupled...") | BEHAVIOR DIFFERS (architecture, currently in sync) | **Important finding**: Qt does not call into the same shared validation functions — it maintains its own hand-synced copy. Text/logic currently match exactly, but there is no structural guarantee they stay in sync; a future edit to one side's wording/logic won't propagate to the other. Maintenance-risk finding, not a currently-active bug.
| **Required-field validation (`_validate_settings`)** | Full pre-run check across always-required fields, fan-required fields (gated on enable), inlet2/outlet2-required fields (gated on enable), steady-state-required fields (gated on sim-type), and per-enabled-monitoring-point fields (app.py L755-849+, invoked before a run starts) — returns a list of missing-field labels used to block the run with a specific message naming exactly which field(s) are empty | **No equivalent function anywhere in `guvcfd/qtapp/`** (grepped "validate"/"required" case-insensitively — no relevant hits) | MISSING IN PC | **Real functional gap.** Although this check is triggered from Run-tab callbacks in Dash, the fields it validates are 100% Project-Setup fields, and Qt appears to have no pre-run check at all for missing/blank required geometry — a run could be launched with e.g. a blank inlet width with no Dash-equivalent named-field error message.

## 13. Cross-cutting: position-field range/default summary table

| Field | Dash default | Qt default | Room-dimension clamp after load? |
|---|---|---|---|
| Inlet Position 2 (Z) | 2.1 | 1.5 | Dash: yes; Qt: no |
| Outlet Position 2 (Z) | 0.4 | 1.5 | Dash: yes; Qt: no |
| 2nd Inlet Position 1 | 2.0 | 1.5 | Dash: yes; Qt: no |
| 2nd Outlet Position 1 | 2.0 | 1.5 | Dash: yes; Qt: no |
| Monitor Point 2, X | 3.0 (0.75×room X) | 2.0 | Dash: yes; Qt: no |
| Monitor Point 3, X | 1.0 (0.25×room X) | 2.0 | Dash: yes; Qt: no |
| All other position fields | — | — | matches; Dash still uniquely clamps max to actual room size, Qt never does |

**Section summary: ~95 distinct GUI elements catalogued.**

---

# Part 2 — Run Simulations tab

Scope: start/pause/stop/resume, progress, log, settings modal, sweep queue, error handling.

Key source locations:
- Dash layout: `scenario_tab` app.py:2130-2190, `flow_decision_panel` app.py:2052-2068, `phase2_resume_panel` app.py:2076-2088, `simulation_settings_modal` app.py:2209-2311.
- Dash callbacks: app.py:4252-4587 (decision/resume/stop/pause/sweep-launch), app.py:4677-4949 (progress tables + poll), app.py:5099-5185 (legacy single-run poller that still drives the decision panels).
- Dash helpers: `_sealed_room_error`/`_mechanical_ach_only_error` app.py:783-825, `_run_pipeline_thread` app.py:1555-1576, `_handle_flow_convergence_undecided` app.py:1508-1531, `_launch_run`/`_launch_scenario_sweep` app.py:3967-4032, `_scenario_progress_table`/`_single_run_progress_table` app.py:4677-4843.
- Qt: `RunTab` qtapp/run_tab.py (full file), `RunState` qtapp/run_state.py (full file), `SweepState` qtapp/sweep_state.py (full file), `sealed_room_error`/`mechanical_ach_only_error` qtapp/helpers.py:159-185, `simulation_settings_dialog` qtapp/project_setup_tab.py:295-344.

Total elements catalogued: 38.

## 1. Run controls (start / stop / pause)

| # | Element | Dash (server) | Qt (PC) | Verdict | Sync note |
|---|---|---|---|---|---|
| 1 | Start button | `"Start simulations"`, id `scenario-run-btn` | `"Start simulations"` (run_tab.py:70) | MATCH | - |
| 2 | Stop button | `"Stop simulation"`, id `scenario-stop-btn`, label never changes | `"Stop"` (run_tab.py:73) | TEXT DIFFERS | Cosmetic only, not worth syncing.
| 3 | Pause button | `"Pause simulation"`; **relabeled dynamically** to `"Pause simulation"`/`"Continue simulation"` (single run) or `"Pause Sweep"`/`"Continue Sweep"` (multi-combo) by `_poll_scenario` | `"Pause"`; `toggle_pause()` sets text to `"Pause"`/`"Continue"` always, no sweep-specific wording | BEHAVIOR DIFFERS | Minor - Dash distinguishes "sweep" vs "simulation" pause wording; Qt always generic. Low priority.
| 4 | Stop click log line (single run) | `"Stop requested..."` (`_stop_scenario_sweep`, app.py:4554-4556) | No log line at all — `stop_run()` just sets `stop_requested = True` | MISSING IN PC (log confirmation) | Worth syncing lightly.
| 5 | Stop click log line (sweep) | `"Stop requested - the sweep will stop before its next combination..."` (app.py:4557-4559) | Same as #4 — no log line | MISSING IN PC | Same as above.
| 6 | Pause click log line | Narrates the click for both running/continue and single/sweep cases (app.py:4569-4586) | No log line - `toggle_pause()` only flips the flag and button text | MISSING IN PC | Dash always narrates the click in the log; Qt is silent until the next poll shows the new status text.
| 7 | Paused status text | `"Paused - solver suspended in place. Click Continue to resume."` (single) / adds `"(N/M done)"` for sweeps (app.py:4917, 4944-4945) | Same wording for single-run case; **never shows sweep combo counts** (run_tab.py:238) | TEXT DIFFERS for sweep case | Sweep-paused status in Qt doesn't report combo counts.
| 8 | "Simulation Settings..." button | `"Simulation settings…"` (app.py:2151-2152) | `"Simulation Settings..."` (run_tab.py:66-68) | MATCH (minor ellipsis-character difference) | Cosmetic.
| 9 | Z/ACH list inputs | Both **required** — blank list triggers `"Enter at least one Z value and one ACH value."` (app.py:4465-4467) | Blank **falls back to the project's current single value** (`_parse_lists`, run_tab.py:125-134), documented in Qt's own docstring as a deliberate improvement | BEHAVIOR DIFFERS (intentional, documented) | Fine as-is per Qt's own docstring.
| 10 | Combo-count helper text | `f"{n} combination{'s' if n != 1 else ''} ({len(z_values)} Z x {len(ach_values)} ACH)."` | Identical format string (run_tab.py:136-144) | MATCH | —

## 2. Progress display (single run)

| # | Element | Dash | Qt | Verdict | Sync note |
|---|---|---|---|---|---|
| 11 | Overall status line | `"Running... (1/1 combination)"`, `"Finished. 1/1 succeeded."`, `"Failed - see log below."`, `"Stopped."`, each **prefixed** with `"Total run time: M:SS"` | `"Running..."`, `"Finished."`, `f"Failed: {state.error}"`, `"Stopped."` — **no elapsed/total-run-time line at all** | MISSING IN PC (elapsed time) + TEXT DIFFERS (Qt's error text includes the message directly) | Elapsed-time display worth syncing. Qt's inline error text is arguably *better* (shows the message directly) - worth carrying back into Dash too.
| 12 | Stage/current-phase text | Setup / Flow field calc / Decay sim / Phase 1 / Phase 2 / Post-processing | Same six labels, same underlying log-line markers | MATCH | Genuinely well-synced.
| 13 | "Simulation time step" progress line | `f"Simulation time step {cur_val:.4g} of {target:.4g} ({pct}%)"` | Identical format string | MATCH | Byte-for-byte identical.
| 14 | ETA line | `f"Expected finish of this step in {_format_mmss(...)}"` — shows `H:MM:SS` once past an hour | `f"...{m}:{s:02d}"` — **no hour rollover**, always `M:SS` | BEHAVIOR DIFFERS (minor) | For runs >1hr remaining Dash shows `H:MM:SS`, Qt shows raw minutes (e.g. "125:30" instead of "2:05:30").
| 15 | Live per-stream status | `f"[{k}] {live_status[k]}"` sorted by key | Same format, sorted by key | MATCH | Same key/format convention on both sides.
| 16 | Elapsed-time display ("Elapsed: M:SS") | Legacy-only, not surfaced on the visible scenario_tab | Not present | Effectively MATCH | No action needed.

## 3. Simulation Progress table (headline metrics)

| # | Element | Dash | Qt | Verdict | Sync note |
|---|---|---|---|---|---|
| 17 | Table columns | Z, ACH, Status, **Est. time to finish**, Total reduction %, Measured ACH eff. %, Measured UV eff. %, Mechanical mixing eff. %, Est. ACH /hr, Est. eACH /hr | Z, ACH, Status, Reduction %, Measured ACH eff. %, Measured UV eff. %, Mechanical mixing eff. %, Est. ACH /hr, Est. eACH /hr | MISSING IN PC ("Est. time to finish" column entirely absent) + TEXT DIFFERS ("Total reduction %" vs "Reduction %") | The missing ETA column is the more material gap.
| 18 | Status cell for a not-yet-finished combo (sweep) | Shows the actual running phase ("Flow field calc"/"Phase 1"/"Phase 2"/"Decay sim"/"Running") for a combo currently executing, `"pending"` only if not started | Always `"pending"` for any combo without a results-entry yet, whether actively running or just queued | MISSING IN PC (per-combo live stage) | Real behavior gap during a multi-combo sweep.
| 19 | Metrics cell when combo finished but a metric is `None` | `"n/a"` vs `""` when the combo hasn't started at all — disambiguated | Always returns `""` for `None`, never distinguishes "not started" from "finished but N/A" | TEXT DIFFERS | Minor but real ambiguity in Qt.
| 20 | Status cell for an errored combo | `f"error: {entry['detail']}"` | Same format | MATCH | —
| 21 | Header note above table | `"Per-monitoring-point results stay under Analysis of Results - this table is room-average headline numbers only."` | Verbatim identical string | MATCH | —

## 4. Log / status text

| # | Element | Dash | Qt | Verdict | Sync note |
|---|---|---|---|---|---|
| 22 | Log panel | Last 300 lines shown per poll, capped at 5000 stored | Up to 20000 displayed blocks, 5000 stored | BEHAVIOR DIFFERS (display window size) | Different UI paradigm (re-render vs incremental append) - worth knowing the visible scrollback differs by ~65x.
| 23 | Generic run failure log line | `f"ERROR: {e}"` | Identical | MATCH | —
| 24 | Stop confirmation detail (what stage the stop landed on) | `_run_log(f"Stopped: {e}")` where `e` includes the stage, e.g. `"Stopped before pimpleFoam."` | **No log line at all** — `except StoppedByUser: state.status = "stopped"` discards the specific message | MISSING IN PC | Real information loss — Dash tells the user exactly which stage the stop landed on; Qt drops that detail entirely.
| 25 | Sweep failure line | `f"ERROR: {e}"`, per-combo isolation via `on_combo_done` | Same pattern, same underlying shared `scenario_runs` functions | MATCH | Both delegate to the same functions, so identical by construction.

## 5. Sealed-room / mechanical-ACH-only validation errors

| # | Element | Dash | Qt | Verdict | Sync note |
|---|---|---|---|---|---|
| 26-29 | Sealed-room (steady-state / no fan), mechanical-ACH-only (steady-state / sealed room) error text | 4 messages, verbatim (app.py:796-824) | Byte-for-byte identical (helpers.py:164-184) | MATCH | Verified identical but duplicated, not shared — same drift risk noted in Part 1 §12.
| 30 | Where the check runs (sweep) | Checked once against the whole ACH list before launch; rejected outright if any value is invalid | Checked per-ACH-value in a loop before launch; message prefixed `f"ACH={ach}: {error}"` | MATCH (behaviorally) | Minor TEXT DIFFERS (Qt's per-value prefix), not confusing either way.

## 6. Flow-convergence-undecided / Phase-1-extrapolation-undecided resume UX

| # | Element | Dash | Qt | Verdict | Sync note |
|---|---|---|---|---|---|
| 31 | Decision panel itself | `flow_decision_panel`: title `"Flow convergence needs a decision"`, 3 buttons (`"Continue this many more iterations"` + input, `"Accept current state and proceed"`, `"Stop (leave as-is, decide later)"`) | **Does not exist.** Per `run_state.py`'s own docstring: "...surfaces it as an error in the log instead of an interactive decision panel - rerun, or use the web app to resume it." | MISSING IN PC (by design, explicitly documented) | **The single biggest behavioral gap on the Run tab.** Confirmed deliberate/known, but still the most consequential item for a user: no in-app path to continue/accept in Qt at all.
| 32 | Decision panel status text (paused, not error) | `"Paused - awaiting your decision (see the panel above). Not an error, not hung."` | N/A — produces `status = "error"`, `f"Failed: {state.error}"` with the raw `FlowConvergenceUndecided` message | MISSING IN PC | Directly consequential: Dash explicitly reassures the user this is *not* an error; Qt shows the exact same condition as a hard failure.
| 33 | Phase2-resume panel | `phase2_resume_panel`: title `"An unfinished steady-state run was found"`, buttons `"Resume (skip completed steps)"` / `"Discard and start over"` | Does not exist — same simplification as #31 | MISSING IN PC (by design, explicitly documented) | Same class of gap — unfinished steady-state runs in Qt have no in-app resume path.
| 34 | Overwrite-confirmation dialog | `dcc.ConfirmDialog`, shown when `case_dir` already has simulation data: `"...Running will regenerate the mesh and overwrite the case directory in place - existing results may be lost. Continue anyway?"` | **Does not exist at all.** `start_run()` goes straight from validation to `mkdir(exist_ok=True)` and launch — no check for existing results, no warning | MISSING IN PC | **Not flagged in either Qt docstring — looks like an unintentional omission, not a documented simplification. Genuine data-loss risk.**

## 7. Sweep/batch-run architecture

| # | Element | Dash | Qt | Verdict | Sync note |
|---|---|---|---|---|---|
| 35 | Sweep support in-GUI | Fully in-GUI via `scenario_runs.run_sweep`/`run_decay_sweep`; a 1-combination "sweep" special-cased through the single-run path to retain decision/resume support | Also fully in-GUI, calling the identical `scenario_runs` functions; 1-combination case also special-cased through `run_state.launch_run` | MATCH (architecturally) | Contrary to the initial hypothesis, sweep/batch controls are NOT missing from Qt or handled by a separate script. The only real difference is items #31/#33 — Qt's 1-combo case gets no decision-panel/resume support either way (Qt has none at all, sweep or single).
| 36 | Concurrency limit | `_MAX_CONCURRENT_Z` from `scenario_runs.py` | Same constant, imported directly | MATCH | Shared source of truth.
| 37 | Sweep summary table population | Iterates combos in `sweep_combinations()` order | Same order, same underlying function | MATCH | —
| 38 | Stale-results clearing on new sweep launch | Explicitly clears results stores to prevent the Analysis tab showing a stale result | N/A — Qt's Analysis tab is a separate widget with its own load step, not auto-populated during a run | N/A (different architecture) | No action needed.

**Section summary: 38 elements catalogued.**

---

# Part 3 — Analysis of Results tab + app shell (menus, Advanced Settings)

Scope: results table, charts, report export, ParaView launch, top-level File/Help menus, the global Advanced Settings dialog, and top-level alerts.

Sources read:
- Dash: `guvcfd/app.py` (analysis_tab ~L2446-2460, settings_modal ~L2462-2923, grid_align modals ~L2925-2951, app.layout/menu ~L2953-3021, summary builders ~L2314-2443, callbacks ~L3230-3560 and results-autoload at L4432/L5111-5185), `guvcfd/app_settings.py`, `guvcfd/result_figures.py`, `guvcfd/report.py`, `guvcfd/help_content.py`.
- Qt: `guvcfd/qtapp/main_window.py`, `guvcfd/qtapp/settings_dialog.py`, `guvcfd/qtapp/analysis_tab.py`, `guvcfd/qtapp/charts.py`.

## 1. App shell — File menu

| # | Element | Dash (server) | Qt (PC) | Verdict | Sync note |
|---|---|---|---|---|---|
| 1 | Menu label | `"File"` dropdown | `"&File"` native menu | MATCH | Expected platform difference (web dropdown vs native mnemonic).
| 2 | "New project from .guv" menu item | **Absent from the File menu** — only a `"Load .guv file..."` button embedded in the Project Setup tab body | `"New Project from .guv file..."` action in the File menu | MISSING IN SERVER | Worth syncing: a user scanning Dash's File menu for "how do I load a new room" won't find it there — it's tab-scoped only.
| 3 | "Open Project..." | `"Open Project..."` (id `menu-open`) | `"Open Project..."` (Ctrl+O) | MATCH (text) | Qt adds a native accelerator — expected platform difference.
| 4 | "Open Recent" submenu (MRU list) | **Does not exist.** No recent-projects tracking anywhere in app.py. | Live-populated `"Open Recent"` submenu, disabled `"(no recent projects)"` when empty | MISSING IN SERVER | Real feature gap, not cosmetic — worth porting if the underlying store is UI-agnostic.
| 5 | "Save Project" | `"Save Project"` (id `menu-save`) | `"Save Project"` (Ctrl+S) | MATCH (text) | Both backfill missing per-project OpenFOAM settings via `capture_openfoam_settings` at save time — confirm Qt does the same (tab-scoped code, flagged for the Project-Setup section since the *behavior parity* matters for this shell feature).
| 6 | "Save Project As..." | `"Save Project As..."` | `"Save Project As..."` (Ctrl+Shift+S) | MATCH | —
| 7 | Current-project display | `"Project file: "` + `"Untitled project"`, updated on Open/Save/Save As | **None** — window title stays `"GUV-CFD v{version}"`, never updated | MISSING IN PC | Minor but real — a Qt user has no persistent on-screen reminder of which project is currently loaded.

## 2. App shell — Settings entry point

| # | Element | Dash | Qt | Verdict | Sync note |
|---|---|---|---|---|---|
| 8 | Button/action label | `"Settings"` (own top-level control between File and Help) | `"Settings..."` (menu-bar action) | TEXT DIFFERS | Trivial - the trailing ellipsis is the Windows convention for "opens a dialog"; cheap one-word sync. Placement already matches well.

## 3. Advanced Settings dialog (global `advanced_settings.json`, 32 keys)

**Headline finding: field coverage is complete on both sides.** All 32 keys in `ADVANCED_SETTINGS_DEFAULTS` have a corresponding input in both apps. The "JSON-only, no new UI" project-history note refers to the *per-project* `.guvcfd` overrides (neither app exposes a GUI for those), not this global dialog — that symmetry is fine. Differences that *are* real:

| # | Element | Dash | Qt | Verdict | Sync note |
|---|---|---|---|---|---|
| 9 | Dialog title | `"Advanced Settings"` | `"Advanced Settings"` | MATCH | —
| 10 | Field grouping / section headers | Grouped under 9 bold section headers with explanatory paragraphs (Convergence tolerances, Flow oscillation acceptance, Ventilation delivery check, Solver stability, Phase 1 readiness, Residence-time-scaled deltaT, Steady-state time-step retention, Decay-mode solver timing, Mesh & zone resolution, Scenario sweep troubleshooting) | **Flat form** — one continuous list, no section headers or grouping, in dict-iteration order (doesn't match Dash's logical grouping) | MISSING IN PC | Real usability gap for a 32-field dialog — Dash's grouping+prose makes it navigable; Qt's flat list is harder to scan. Per-field explanation still exists as tooltips — it's the *organization* that's missing, not the content.
| 11 | Per-field help text depth | Long-form paragraph tooltips, often citing specific validated numbers/test results | Tooltips exist but are noticeably shorter/more clipped paraphrases of the same facts | TEXT DIFFERS (content mostly equivalent) | Low priority — Qt is appropriately terser for a tooltip vs. an always-visible paragraph.
| 12 | "Reset to defaults" | `"Reset to defaults"` button in the modal footer — resets all fields in-dialog without saving | **No equivalent button** — only Save/Cancel | MISSING IN PC | Real feature gap — a Qt user who wants to revert to shipped defaults has no in-dialog way to do it.
| 13 | Save-time validation guard | **Refuses to save** and shows an inline error if `t-infinity-early-stop-enabled` and `keep-all-timesteps` are both on, citing a known (since-fixed) directory-naming bug | **Saves unconditionally**, no such check | BEHAVIOR DIFFERS — MISSING IN PC (data-integrity guard) | **The most consequential single gap found in this section.** Worth porting the same guard into `SettingsDialog._save`.
| 14 | Save confirmation | Inline `"Saved."` on success | No confirmation — dialog just closes | MISSING IN PC | Minor UX gap.
| 15 | Re-populate values fresh on every open | Re-reads `load_advanced_settings()` every time the modal opens | Re-reads once per dialog construction (a fresh dialog instance every open) — same effect | MATCH (behaviorally equivalent, different mechanism) | —
| 16 | Cancel behavior | Closes without saving | Closes without saving | MATCH | —

## 4. Analysis of Results tab — buttons / status

| # | Element | Dash | Qt | Verdict | Sync note |
|---|---|---|---|---|---|
| 17 | Load button | `"Load results.json..."`, defaults to session case-dir or Project Setup's case-dir field (2-tier fallback) | `"Load results.json..."`, defaults through a 3-tier fallback chain | MATCH (text); Qt's fallback is slightly richer | Low priority.
| 18 | Export button | `"Export report (.docx)..."` | `"Export Word Report..."` | TEXT DIFFERS | Same feature, different wording — cheap to sync.
| 19 | ParaView button | `"Open in ParaView"` | `"Open in ParaView"` | MATCH | —
| 20 | Disabled-state handling | Always enabled; clicking with nothing loaded shows an inline status message | Export/ParaView start disabled, only enabled once a load succeeds | BEHAVIOR DIFFERS | Reasonable platform-idiomatic difference, not worth forcing to match.
| 21 | Status line after load | `f"Loaded {name}"` — filename only | `f"Loaded {results_path}"` — full path | TEXT DIFFERS | Minor granularity difference.
| 22-23 | Export/ParaView-without-data guard message | Inline status text explaining what to do first | N/A — button simply disabled | BEHAVIOR DIFFERS | Not a bug, different but equally valid UX pattern.
| 24 | Missing `run_settings.json` (ParaView) | Inline status: "...rerun a full simulation here to enable the ParaView preset." | `QMessageBox.warning`: "...rerun a full simulation here first." | TEXT DIFFERS | Same condition, different wording and presentation (inline vs modal) — worth aligning wording.
| 25 | Export success message | `f"Report saved to {name}"` — filename only, inline | `QMessageBox.information`: `f"Saved to {path}"` — full path, popup | TEXT DIFFERS | Same pattern as #21.
| 26 | Export failure message | `f"Failed to export report: {e}"` | `QMessageBox.critical`, raw exception text only, no prefix | TEXT DIFFERS | Functionally equivalent, wording differs.
| 27 | ParaView failure message | `f"Failed to open ParaView: {e}"` | `QMessageBox.critical("Failed to open ParaView", str(e))` | MATCH (same core message, inline vs dialog title split) | —
| 28 | ParaView success message | Descriptive — names exactly which views were set up (log-scale volume T + streamlines, etc.) | **No success message at all** — returns silently on success | MISSING IN PC | Real gap: Dash tells the user what was set up; Qt gives zero feedback that the launch even worked.
| 29 | "No results.json at target dir" | No distinct message (manual load always starts from an existing picked file) | `QMessageBox.warning("No results", ...)`, also fires from post-run auto-load | Structural difference, not a content mismatch | —

## 5. Analysis tab — auto-load / tab-switch on run completion (shell-adjacent)

| # | Element | Dash | Qt | Verdict | Sync note |
|---|---|---|---|---|---|
| 30 | Auto-load results when a run finishes | Loads into stores once `status == "done"`, silently no-ops if the read fails | Calls `load_case_dir(state.case_dir)` | MATCH (both auto-load) | —
| 31 | Auto-switch to the Analysis tab on completion | **Does not switch tabs** — user stays on Run Simulations even though results just loaded | Explicitly switches the visible tab to Analysis | BEHAVIOR DIFFERS — MISSING IN SERVER | Genuine UX gap — a Dash user not watching the Run tab has no on-screen cue results are ready. Worth porting (conditional `active_tab` output).
| 32 | Post-completion status feedback | Run tab's own status text (out of scope) | Native status bar message: `"Run finished - results loaded from {case_dir}"` | Different mechanism | Platform-appropriate, not flagged as a gap.
| 33 | Run/Sweep failure alert | In-tab log/status only | App-level `QMessageBox.critical("Run failed"/"Sweep failed", ...)` | MISSING IN SERVER (as a global alert) | Minor — Dash's pattern is arguably less naggy, not necessarily a regression; noted since shell-level alerts were in scope.

## 6. Analysis charts (`result_figures.py` vs `qtapp/charts.py`)

Both read the *same* `results.json` shape, but render very different amounts of content from it.

### 6a. Decay-mode chart

| # | Element | Dash (`decay_figure`) | Qt (`ResultsChart.plot_decay`) | Verdict | Sync note |
|---|---|---|---|---|---|
| 34 | Traces plotted | **3 traces**: actual CFD curve, `"Ventilation ACH only"` idealized reference (dashed), `"Well-mixed, ACH+eACH_uv"` idealized reference (dashed) | **1 trace only**: raw CFD curve, markers only | MISSING IN PC | Significant content gap — the two idealized reference curves are the whole point of the plot's design (visually showing how much imperfect mixing slows disinfection vs. the idealized box model). Qt shows only the raw curve with nothing to compare it against.
| 35 | Y-axis label | `"volAverage(T)"` | `"Room-average concentration T"` | TEXT DIFFERS | Same quantity — Qt's arguably more readable; still worth picking one.
| 36 | X-axis label | `"Time (s)"` | `"Time (s)"` | MATCH | —
| 37 | Y scale | Log | Log | MATCH | —
| 38 | Chart title | None | `"Decay curve"` | MISSING IN SERVER (trivial) | Cosmetic only.
| 39 | "No curve data" fallback | Explanatory placeholder message for trimmed sweep reports, pointing the user at the combination's own results subfolder | **No equivalent** — silently plots nothing (empty axes) | MISSING IN PC | Real gap for the sweep-report use case.

### 6b. Steady-state-mode chart

| # | Element | Dash (`steady_state_figure`) | Qt (`ResultsChart.plot_steady_state`) | Verdict | Sync note |
|---|---|---|---|---|---|
| 40 | Y-axis quantity | **Normalized**: `T (% of phase 1 steady state)` | **Raw**: plots `T` directly, no normalization | BEHAVIOR DIFFERS | Meaningful gap — Dash's normalization is what makes phase 1→phase 2's relative reduction readable at a glance; Qt requires the user to do that math themselves.
| 41 | X-axis quantity/label | Phases plotted on **one continuous shifted timeline**, labeled `"Time (s)"` | Phases plotted on **independent, unshifted axes overlaid**, labeled `"Iteration"` | TEXT + BEHAVIOR DIFFER | Two compounding issues: (a) label mismatch on the same underlying field (worth checking which is actually correct); (b) Qt doesn't shift phase 2 to start where phase 1 ends, so the UV-on transition is much harder to see.
| 42 | UV-on transition marker | Explicit vertical dashed line at the phase transition | **None** | MISSING IN PC | —
| 43 | Steady-state reference lines | Horizontal lines at Phase 1 (100%) and Phase 2 steady-state % | **None** | MISSING IN PC | —
| 44 | Trailing-window shading | Shades the moving-average window used to compute T_ss, plus a dotted mean line | **None** | MISSING IN PC | —
| 45 | Trace labels/legend | `"Phase 1 (no UV)"`, `"Phase 2 (UV on)"` | Same text | MATCH (text) | —
| 46 | "No curve data" fallback | Same explanatory placeholder as decay mode | **None** — silently plots nothing | MISSING IN PC | Same gap as #39, for steady-state sweep reports.
| 47 | Chart title | None | `"Steady-state buildup"` | MISSING IN SERVER (trivial) | Cosmetic only.

**Overall chart verdict**: the Qt `ResultsChart` is a substantially simplified version of the Dash Plotly figures — same source data, far less visual analysis content. **The single largest area of drift found in the whole audit.**

## 7. Results table / summary content

| # | Element | Dash | Qt | Verdict | Sync note |
|---|---|---|---|---|---|
| 48 | Table structure | Stack of key:value `Div` rows | Literal `QTableWidget(0, 2)`, explicit `["Metric", "Value"]` headers | Structural difference only | Fine — equivalent widget choices.
| 49 | Fields shown (decay mode) | Up to 10 rows depending on availability, plus monitoring-location rows and 1-3 explanatory notes | Fixed 13-row allowlist filtered to whatever keys exist | MISSING IN PC | Qt entirely lacks: fluence rate, target T_ss, source injection rate, per-phase T_ss/CV detail, monitoring-location rows, and **all explanatory notes** — including the mixing-uniformity warning, which exists specifically to flag a room that isn't well-mixed.
| 50 | Fields shown (steady-state mode) | Similar richness plus provenance suffixes (e.g. "using extrapolated T∞") | Same fixed 13-row list, doesn't distinguish scenario type — `target_T_ss`, `injection_rate_total`, per-phase CV/plateau detail, provenance suffix all absent | MISSING IN PC | Same pattern as #49.
| 51 | Unit/percentage formatting | Explicit per-field unit suffixes and correct scaling — `mixing_efficiency`/`mixing_efficiency_corrected`/`spatial_cov_final` are 0-1 fractions in the JSON, ×100 with `%` appended; rate fields get `" /hr"`, fluence gets `" µW/cm²"` | **Blanket `f"{value:.4g}"` with no unit suffix and no scaling for any row** — the three fraction fields print as bare decimals (e.g. `0.834` instead of `83.4%`) | MISSING IN PC / **real display bug** | Concrete, fixable correctness issue — a user reading the Qt table can easily misread `0.834` as "0.8%" or not realize it needs ×100. Recommend Qt apply the same per-field unit/scale rules, or at minimum fix the fraction→percent scaling for the two mixing-efficiency fields and spatial CoV.
| 52 | Metric label wording | e.g. `"eACH_uv, CFD-fit (nominal ventilation ACH)"`, `"Measured UV eff. %"`, `"Reduction"` | e.g. `"eACH_uv, CFD-fit (nominal baseline)"`, `"Mixing efficiency"`, `"Steady-state reduction (%)"` | TEXT DIFFERS throughout | The two label sets were clearly written independently — every label differs at least slightly even where the underlying field is identical. Low priority individually, but makes cross-referencing the two apps' output harder than it should be.

## 8. Report generation (.docx)

| # | Element | Dash | Qt | Verdict | Sync note |
|---|---|---|---|---|---|
| 53 | Report content/generation | `generate_report_docx(case_dir, path)` from shared `guvcfd/report.py` | Same function, same import | **MATCH** | Positive finding — the exported Word document is byte-for-byte the same regardless of which UI triggered it, since both call the identical shared function. No drift risk here as long as `report.py` itself isn't forked.
| 54 | Default output filename | Prefers the run's own `guv_path` from `run_settings.json`, falls back to the Setup tab's open project, then the case-dir name — always `f"{stem}_report.docx"` | Always `f"{Path(case_dir).name}_report.docx"` — no `run_settings.json`/`guv_path` lookup at all | BEHAVIOR DIFFERS | Minor but real — Dash's default filename tracks back to the actual `.guv` project name (e.g. `"PatientWard_report.docx"`); Qt's default is always the (often auto-generated) case directory name.

## 9. Help menu

| # | Element | Dash | Qt | Verdict | Sync note |
|---|---|---|---|---|---|
| 55 | Menu items | `"About"`, `"License"`, `"References"`, `"OpenFOAM Notes"`, rendered from `help_content.py` | Identical 4 items, same order, same `help_content` module | **MATCH — exactly** | Genuinely good: both UIs literally import the same `guvcfd/help_content.py` module, so this content is guaranteed identical and can't drift as long as that stays shared. `REFERENCES` content itself is currently just a placeholder in both — not a UI gap, just unfinished content shared by both.

**Section summary: ~55 elements catalogued.** Verdict tally: 12 MATCH, 14 TEXT DIFFERS, 9 BEHAVIOR DIFFERS, 4 MISSING IN SERVER, 16 MISSING IN PC.

