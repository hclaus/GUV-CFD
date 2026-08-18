"""Run Simulations tab: launch a Z x ACH sweep (a single Z and single ACH
is just a 1-combination sweep, same as guvcfd.app), watch live progress/
log, pause/stop it - the native equivalent of guvcfd.app's Run Simulations
tab.

"Continue" (2026-08-18) resumes a stopped single Z/ACH run in place -
see run_state.probe_resumable_state/launch_continue for exactly which
stopped states it covers (flow convergence, steady-state Phase 1/Phase 2)
and which it doesn't yet (a sweep of more than one combination - use
"Extend / modify simulations..." instead; a decay run stopped before its
own first completion - continue_decay only extends an already-finished
one further).
"""
import time
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..app_settings import load_advanced_settings, merge_project_openfoam_settings
from ..report import combo_summary_metrics
from ..scenario_runs import _MAX_CONCURRENT_SOLVES, sweep_combinations
from . import helpers, run_state, sweep_state

_TABLE_HEADERS = ["Z", "ACH", "Status", "Reduction %", "Measured ACH eff. %", "Measured UV eff. %",
                   "Mechanical mixing eff. %", "Est. ACH /hr", "Est. eACH /hr"]

# 25% bigger than this app's ~9pt/~28px native default, bold, with a
# raised/beveled ("3D") gradient look - the flat native buttons were hard
# to see against the surrounding form (2026-08-11 user report).
_BUTTON_STYLE = """
QPushButton {
    font-weight: bold;
    font-size: 12pt;
    padding: 8px 20px;
    min-height: 35px;
    border: 2px outset #8a8a8a;
    border-radius: 5px;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #f0f0f0, stop:1 #c4c4c4);
    color: #1a1a1a;
}
QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #fafafa, stop:1 #d2d2d2);
}
QPushButton:pressed {
    border-style: inset;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #b0b0b0, stop:1 #d0d0d0);
}
QPushButton:disabled {
    color: #8a8a8a;
    border: 2px outset #b5b5b5;
    background: #d8d8d8;
}
"""


def _format_mmss(seconds):
    """Matches guvcfd.app's own _format_mmss exactly - H:MM:SS once past
    an hour, else M:SS."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class RunTab(QWidget):
    run_finished = Signal()  # emitted whenever the active run/sweep reaches done/error/stopped

    def __init__(self, project_setup_tab, parent=None):
        super().__init__(parent)
        self.project_setup_tab = project_setup_tab
        self.state = run_state.RunState()
        self.sweep_state = sweep_state.SweepState()
        self._active = None  # "single" or "sweep" - which state currently drives the UI
        self._last_log_text = ""

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Runs the current project's setup once per Z x ACH combination (every Z with every "
            "ACH), each into its own subfolder under the project directory. The flow field is "
            "converged once per distinct ACH and reused for every Z at that ACH, so a longer Z "
            "list at a fixed ACH is much cheaper than it looks. A single Z and a single ACH is "
            "just a 1-combination run - no separate mode to pick.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: gray;")
        layout.addWidget(intro)

        list_form = QFormLayout()
        self.z_values_edit = QLineEdit()
        self.z_values_edit.setPlaceholderText("e.g. 2, 6 (blank = this project's current Z)")
        self.ach_values_edit = QLineEdit()
        self.ach_values_edit.setPlaceholderText("e.g. 3, 6 (blank = this project's current ACH)")
        self.z_values_edit.textChanged.connect(self._update_combo_count)
        self.ach_values_edit.textChanged.connect(self._update_combo_count)
        list_form.addRow("Z values (comma-separated)", self.z_values_edit)
        list_form.addRow("ACH values (comma-separated)", self.ach_values_edit)
        layout.addLayout(list_form)
        self.combo_count_label = QLabel("")
        self.combo_count_label.setStyleSheet("color: gray;")
        layout.addWidget(self.combo_count_label)

        btn_row = QHBoxLayout()
        sim_settings_btn = QPushButton("Simulation Settings...")
        sim_settings_btn.setToolTip("Simulation type, and solver run-duration settings for this project.")
        sim_settings_btn.setStyleSheet(_BUTTON_STYLE)
        sim_settings_btn.clicked.connect(lambda: self.project_setup_tab.simulation_settings_dialog.exec())
        btn_row.addWidget(sim_settings_btn)
        self.run_btn = QPushButton("Start simulations")
        self.run_btn.setStyleSheet(_BUTTON_STYLE)
        self.run_btn.clicked.connect(self.start_run)
        btn_row.addWidget(self.run_btn)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setStyleSheet(_BUTTON_STYLE)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_run)
        btn_row.addWidget(self.stop_btn)
        self.continue_btn = QPushButton("Continue")
        self.continue_btn.setToolTip(
            "Resume a stopped single Z/ACH run from where it left off (flow convergence, or "
            "steady-state Phase 1/Phase 2), instead of starting over. For a sweep (more than one Z/ACH "
            "combination), or a decay run stopped before its own first completion, use "
            "\"Extend / modify simulations...\" instead.")
        self.continue_btn.setStyleSheet(_BUTTON_STYLE)
        self.continue_btn.clicked.connect(self.continue_run)
        btn_row.addWidget(self.continue_btn)
        extend_btn = QPushButton("Extend / modify simulations...")
        extend_btn.setStyleSheet(_BUTTON_STYLE)
        extend_btn.clicked.connect(self.open_extend_dialog)
        btn_row.addWidget(extend_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.runtime_label = QLabel("")
        layout.addWidget(self.runtime_label)
        self.status_label = QLabel("Idle.")
        layout.addWidget(self.status_label)
        self.progress_label = QLabel("")
        layout.addWidget(self.progress_label)

        layout.addWidget(_section_label("Simulation Progress"))
        note = QLabel("Per-monitoring-point results stay under Analysis of Results - this table "
                       "is room-average headline numbers only.")
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)
        self.table = QTableWidget(0, len(_TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(_TABLE_HEADERS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMaximumHeight(220)
        layout.addWidget(self.table)

        layout.addWidget(_section_label("Running now"))
        self.live_status_label = QPlainTextEdit()
        self.live_status_label.setReadOnly(True)
        self.live_status_label.setStyleSheet("background: rgba(127,127,127,0.08); font-family: monospace;")
        # Fixed to fit _MAX_CONCURRENT_SOLVES lines (the most concurrent
        # solves a sweep ever runs at once, across every stage - see
        # scenario_runs.py's own docstring) rather than the old flat 70px,
        # which cut off a line or two as soon as more than ~4 combinations
        # were running concurrently. Still scrolls internally beyond that.
        line_height = self.live_status_label.fontMetrics().lineSpacing()
        self.live_status_label.setFixedHeight(line_height * _MAX_CONCURRENT_SOLVES + 12)
        layout.addWidget(self.live_status_label)

        layout.addWidget(_section_label("Log"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(20000)
        layout.addWidget(self.log_view, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._poll)

    # -- Z/ACH list handling --

    def _parse_lists(self):
        """(z_values, ach_values) from the list fields - a blank field
        falls back to this project's single current value (unlike
        guvcfd.app, which requires both fields filled - defaulting to the
        project's own value avoids redundant re-entry for the common
        1-combination case)."""
        tab = self.project_setup_tab
        z_values = helpers.parse_number_list(self.z_values_edit.text()) or [tab.get_value("z-value")]
        ach_values = helpers.parse_number_list(self.ach_values_edit.text()) or [tab.get_value("ach")]
        return z_values, ach_values

    def _update_combo_count(self):
        try:
            z_values, ach_values = self._parse_lists()
        except ValueError as e:
            self.combo_count_label.setText(f"Can't parse: {e}")
            return
        n = len(sweep_combinations(z_values, ach_values))
        self.combo_count_label.setText(f"{n} combination{'s' if n != 1 else ''} "
                                        f"({len(z_values)} Z x {len(ach_values)} ACH).")

    # -- launch/stop/pause --

    def start_run(self):
        tab = self.project_setup_tab
        if tab.room is None or tab.guv_path is None:
            QMessageBox.warning(self, "No project loaded", "Load a .guv project first (Project Setup tab).")
            return
        settings = tab.gather_settings()
        if not settings.get("case-dir"):
            QMessageBox.warning(self, "No project directory", "Set an OpenFOAM project directory first.")
            return
        try:
            z_values, ach_values = self._parse_lists()
        except ValueError as e:
            QMessageBox.warning(self, "Can't parse Z/ACH list", str(e))
            return
        for ach in ach_values:
            error = helpers.sealed_room_error(settings["sim-type"], ach, settings.get("fan-enable"))
            if error:
                QMessageBox.warning(self, "Can't start this run", f"ACH={ach}: {error}")
                return
            error = helpers.mechanical_ach_only_error(settings["sim-type"], ach, settings.get("mech-ach-only"))
            if error:
                QMessageBox.warning(self, "Can't start this run", f"ACH={ach}: {error}")
                return

        missing = helpers.validate_settings(settings)
        if missing:
            QMessageBox.warning(self, "Can't start this run",
                                 "Missing required value(s): " + ", ".join(missing))
            return

        if helpers.case_dir_has_data(settings["case-dir"]):
            reply = QMessageBox.question(
                self, "Overwrite existing results?",
                f"{settings['case-dir']} already has simulation data (results.json and/or solver "
                f"output). Running will regenerate the mesh and overwrite the case directory in "
                f"place - existing results may be lost. Continue anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return

        combos = sweep_combinations(z_values, ach_values)
        Path(settings["case-dir"]).mkdir(parents=True, exist_ok=True)

        self.log_view.clear()
        self.table.setRowCount(0)
        self._last_log_text = ""
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.continue_btn.setEnabled(False)

        if len(combos) == 1:
            self._active = "single"
            z, ach = combos[0]
            settings["z-value"], settings["ach"] = z, ach
            run_state.launch_run(self.state, tab.guv_path, settings["case-dir"], tab.room, settings)
        else:
            self._active = "sweep"
            adv = merge_project_openfoam_settings(settings, load_advanced_settings())
            sweep_state.launch_sweep(self.sweep_state, tab.guv_path, tab.settings_path,
                                      settings["case-dir"], tab.room, settings, adv, z_values, ach_values)
        self.timer.start()

    def continue_run(self):
        """Resume a stopped single Z/ACH run from where it left off,
        instead of start_run's full setup+solve from scratch - see
        run_state.launch_continue/probe_resumable_state for exactly what
        "left off" means and which stopped states are covered.
        """
        tab = self.project_setup_tab
        if tab.room is None or tab.guv_path is None:
            QMessageBox.warning(self, "No project loaded", "Load a .guv project first (Project Setup tab).")
            return
        settings = tab.gather_settings()
        if not settings.get("case-dir"):
            QMessageBox.warning(self, "No project directory", "Set an OpenFOAM project directory first.")
            return
        try:
            z_values, ach_values = self._parse_lists()
        except ValueError as e:
            QMessageBox.warning(self, "Can't parse Z/ACH list", str(e))
            return
        combos = sweep_combinations(z_values, ach_values)
        if len(combos) != 1:
            QMessageBox.warning(
                self, "Can't Continue a sweep",
                "Continue only resumes a single Z/ACH run. For more than one combination, use "
                "\"Extend / modify simulations...\" instead.")
            return
        settings["z-value"], settings["ach"] = combos[0]

        probe = run_state.probe_resumable_state(settings["case-dir"], settings["sim-type"])
        if probe is None:
            QMessageBox.warning(
                self, "Nothing to resume",
                f"{settings['case-dir']} has no stopped run to resume (it may already be finished, or "
                f"never started) - use Start instead.")
            return
        if probe["stage"] == "phases" and not probe["resumable"]:
            QMessageBox.warning(self, "Can't resume this run yet", probe["reason"])
            return

        self.log_view.clear()
        self.table.setRowCount(0)
        self._last_log_text = ""
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.continue_btn.setEnabled(False)
        self._active = "single"
        run_state.launch_continue(self.state, tab.guv_path, settings["case-dir"], tab.room, settings)
        self.timer.start()

    def stop_run(self):
        self._current_state().stop_requested = True
        self.stop_btn.setEnabled(False)

    def open_extend_dialog(self):
        # Lazy import - avoids a circular import at module load time
        # (extend_modal imports scenario_runs/project_status, which don't
        # need anything from this module, but importing it at the top of
        # this file alongside run_state/sweep_state has caused import-order
        # issues in the past for this app's other dialogs).
        from .extend_modal import ExtendModifyDialog
        tab = self.project_setup_tab
        default_dir = tab.case_dir_edit.text() if tab.room is not None else None
        dlg = ExtendModifyDialog(self, default_project_dir=default_dir, parent=self)
        dlg.exec()

    def launch_sweep_from_extend(self, guv_path, settings_path, project_dir, room, settings, adv,
                                  z_values, ach_values):
        """Launch a sweep from the "Extend / modify simulations" dialog -
        same tail-end UI-state setup start_run() does for its own sweep
        branch, just with the guv_path/settings/room/Z/ACH sourced from
        that dialog's own loaded project instead of this tab's own fields
        (which may be a DIFFERENT project than what's currently open in
        Project Setup)."""
        self.log_view.clear()
        self.table.setRowCount(0)
        self._last_log_text = ""
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.continue_btn.setEnabled(False)
        self._active = "sweep"
        sweep_state.launch_sweep(self.sweep_state, guv_path, settings_path, project_dir, room, settings, adv,
                                  z_values, ach_values)
        self.timer.start()

    def launch_extend_from_extend(self, case_dirs, end_time, write_interval):
        """Launch the "Extend specific run(s)" action - reuses sweep_state/
        the sweep table exactly like launch_sweep_from_extend, just via
        sweep_state.launch_extend (sequential continue_decay calls) instead
        of a genuine Z x ACH sweep."""
        self.log_view.clear()
        self.table.setRowCount(0)
        self._last_log_text = ""
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.continue_btn.setEnabled(False)
        self._active = "sweep"
        sweep_state.launch_extend(self.sweep_state, case_dirs, end_time, write_interval)
        self.timer.start()

    def _current_state(self):
        return self.state if self._active == "single" else self.sweep_state

    # -- polling --

    def _poll(self):
        state = self._current_state()
        # Redraw the whole log from state.log every tick, rather than
        # tracking an incremental "how many lines have I already shown"
        # offset - RunState/SweepState.log_fn trims state.log from the
        # FRONT once it exceeds 5000 entries (unbounded log growth on a
        # long run isn't acceptable either), and an incremental offset
        # into a list that can shrink desyncs permanently the moment the
        # log saturates that cap: once len(state.log) plateaus at exactly
        # 5000 (continuous high-volume output, e.g. several concurrent
        # solvers), "len(state.log) > self._last_log_len" (both pinned at
        # 5000) never fires again - confirmed live as a genuine, lasting
        # freeze (not a rendering-speed issue), not just a temporary lag,
        # since nothing ever un-sticks it once both sides plateau at the
        # same value. Matches guvcfd.app's own poller, which was never
        # exposed to this bug in the first place because it always
        # re-renders "\n".join(state["log"][-300:]) wholesale instead of
        # tracking a delta.
        log_text = "\n".join(state.log)
        if log_text != self._last_log_text:
            self._last_log_text = log_text
            scrollbar = self.log_view.verticalScrollBar()
            was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
            self.log_view.setPlainText(log_text)
            if was_at_bottom:
                scrollbar.setValue(scrollbar.maximum())

        if self._active == "single":
            # "Running now" blends the overall progress/ETA line with the
            # per-stream live_status (only ever non-empty during decay
            # mode's concurrent UV-on + UV-off-control pair - see
            # RunState.live_status_fn) - showing ONLY live_status here (an
            # earlier version of this) left it blank for the entire mesh-
            # gen/flow-convergence portion of a run, looking broken/stuck.
            lines = [t for t in (state.progress_text(), state.eta_text()) if t]
            running_now = "\n".join(lines + ([state.live_status_text()] if state.live_status else []))
            self.live_status_label.setPlainText(running_now)
            self.progress_label.setText(f"Stage: {state.stage}" if state.status == "running" else "")
            self._update_single_table(state)
        else:
            self.live_status_label.setPlainText(state.live_status_text())
            self.progress_label.setText("")
            self._update_sweep_table(state)

        status_text = {
            "running": "Running...", "done": "Finished.", "error": f"Failed: {state.error}",
            "stopped": "Stopped.",
        }.get(state.status, state.status)
        self.status_label.setText(status_text)

        # Computed fresh from state.start_time every tick rather than a
        # one-shot value, so it keeps ticking live while running - and,
        # since start_time itself never changes, naturally freezes at the
        # true total the instant nothing further updates it once status
        # leaves "running" (matches guvcfd.app's own _with_total_run_time).
        if state.start_time:
            self.runtime_label.setText(f"Total run time: {_format_mmss(time.time() - state.start_time)}")
        else:
            self.runtime_label.setText("")

        if state.status != "running":
            self.timer.stop()
            self.run_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.continue_btn.setEnabled(True)
            self.run_finished.emit()

    def _update_single_table(self, state):
        tab = self.project_setup_tab
        self.table.setRowCount(1)
        if state.status == "done" and state.results:
            status, metrics = "Finished", combo_summary_metrics(state.results)
        elif state.status == "error":
            status, metrics = f"error: {state.error}", {}
        elif state.status == "running":
            status, metrics = state.stage, {}  # e.g. "Setup"/"Flow field calc"/"Decay sim" - not a bare "running"
        else:
            status, metrics = state.status, {}
        self._set_table_row(0, tab.get_value("z-value"), tab.get_value("ach"), status, metrics)

    def _update_sweep_table(self, state):
        self.table.setRowCount(len(state.combos))
        for i, (z, ach) in enumerate(state.combos):
            entry = state.results.get((z, ach))
            if entry is None:
                status, metrics = ("pending" if state.status == "running" else state.status), {}
            elif entry["status"] == "done":
                status, metrics = "Finished", combo_summary_metrics(entry["detail"])
            else:
                status, metrics = f"error: {entry['detail']}", {}
            self._set_table_row(i, z, ach, status, metrics)

    def _set_table_row(self, row, z, ach, status, metrics):
        def pct(key):
            v = metrics.get(key)
            return f"{v:.1f}%" if v is not None else ""

        def rate(key):
            v = metrics.get(key)
            return f"{v:.4g} /hr" if v is not None else ""

        values = [
            f"{z:g}", f"{ach:g}", status,
            pct("total_reduction_pct"), pct("ach_efficiency_pct"), pct("uv_efficiency_pct"),
            pct("mechanical_mixing_efficiency_pct"), rate("est_ach_per_hr"), rate("est_each_per_hr"),
        ]
        for col, text in enumerate(values):
            self.table.setItem(row, col, QTableWidgetItem(text))


def _section_label(text):
    lbl = QLabel(text)
    lbl.setStyleSheet("font-weight: 600; text-transform: uppercase; font-size: 11px; margin-top: 6px;")
    return lbl
