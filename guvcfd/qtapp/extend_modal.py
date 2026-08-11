"""Extend / modify simulations dialog - the native equivalent of guvcfd.app's
"Extend / modify simulations..." modal (see app.py's own extend-modal block,
2026-08-10, for the reference design this mirrors). Lets a user come back to
an already-run project (possibly not the one currently open in Project
Setup) and either apply a different UV design to the same room, extend/
resume specific already-run combos, or sweep a few more Z/ACH combinations -
all three consume the project_status.json fingerprinting work rather than
blindly redoing everything. Reuses the exact same backend (project_status.py,
scenario_runs.py) the Dash app uses - only the UI layer differs.

load_extend_status/_ach_label_for_dirs are plain, framework-independent
functions (deliberately factored out of the QDialog class) so they're
testable without a QApplication - matches this app's own established
pattern (run_state.py/sweep_state.py/helpers.py) of keeping logic separable
from widgets wherever practical.
"""
import json
from pathlib import Path

from guv_calcs import Project
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from .. import scenario_runs
from ..app_settings import load_advanced_settings, merge_project_openfoam_settings
from ..project_status import clear_ach_bases, load_project_status
from ..wsl_utils import run_wsl_or_raise, wsl_path
from . import helpers

_STATUS_TABLE_HEADERS = ["Z", "ACH", "Status", "Flow", "UV", "Control (ACH)", "Phase 1"]


def _ach_label_for_dirs(ach):
    """Folder-name-safe ACH label - matches scenario_runs._ach_label /
    project_status._ach_label exactly, duplicated here for the same reason
    those two keep separate copies too (a tiny, stable formatting rule,
    not worth cross-module coupling) - see app.py's own identical helper.
    """
    return "sealed" if ach <= 0 else f"{ach:g}"


def load_extend_status(project_dir):
    """Load project_status.json for project_dir, plus (if it names a real
    guv_path/settings_path) the settings dict and Project/room needed to
    actually launch a new sweep - mirrors app._load_extend_status exactly
    (same contract, same return shape), just without touching any of THIS
    app's currently-loaded project/form state (the folder picked here can
    be a different project entirely).

    Returns (status, settings, room, guv_path, settings_path, error) -
    error is a user-facing string, or None on success. An empty (never-
    swept) project_dir is NOT an error - status/combos may legitimately be
    empty; the error case is specifically "there's no status file AND no
    way to load settings for this folder at all".
    """
    project_name = Path(project_dir).name if project_dir else ""
    status = load_project_status(project_dir, project_name)
    if not status.get("combos") and not status.get("guv_path"):
        return status, None, None, None, None, (
            "No recorded status for this folder yet - open its project first (which creates one), "
            "or point this at a folder a sweep has already run in."
        )
    guv_path, settings_path = status.get("guv_path"), status.get("settings_path")
    if not guv_path or not settings_path:
        return status, None, None, None, None, (
            "This folder's status file is missing its guv_path/settings_path."
        )
    try:
        with open(settings_path) as f:
            settings = json.load(f)
        project = Project.load(guv_path)
        room = next(iter(project.rooms.values()))
    except Exception as e:
        return status, None, None, guv_path, settings_path, f"Failed to load {guv_path}/{settings_path}: {e}"
    return status, settings, room, guv_path, settings_path, None


def scratch_dirs_for_status(project_dir, status):
    """Every _base_ACH*/_control_ACH*/_phase1_ACH* folder this project's
    recorded ach_bases entries point at, filtered to ones that still exist
    on disk - the exact list a cleanup click would remove (see
    _cleanup_scratch_dirs), matching app._extend_ach_scratch_dirs.
    """
    ach_labels = sorted((status.get("ach_bases") or {}).keys())
    dirs = [f"{project_dir}/_base_ACH{label}" for label in ach_labels]
    dirs += [f"{project_dir}/_control_ACH{label}" for label in ach_labels]
    dirs += [f"{project_dir}/_phase1_ACH{label}" for label in ach_labels]  # steady-state only; no-op if absent
    return [d for d in dirs if Path(d).exists()]


class ExtendModifyDialog(QDialog):
    def __init__(self, run_tab, default_project_dir=None, parent=None):
        super().__init__(parent)
        self.run_tab = run_tab
        self.setWindowTitle("Extend / modify simulations")
        self.resize(760, 640)

        self._status = None
        self._project_dir = None
        self._project_name = None
        self._settings = None
        self._room = None
        self._guv_path = None
        self._settings_path = None
        self._action = None

        layout = QVBoxLayout(self)

        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit(default_project_dir or "")
        dir_row.addWidget(self.dir_edit, 1)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_dir)
        dir_row.addWidget(browse_btn)
        load_btn = QPushButton("Load")
        load_btn.clicked.connect(self._load_folder)
        dir_row.addWidget(load_btn)
        layout.addLayout(dir_row)

        self.status_table = QTableWidget(0, len(_STATUS_TABLE_HEADERS))
        self.status_table.setHorizontalHeaderLabels(_STATUS_TABLE_HEADERS)
        self.status_table.horizontalHeader().setStretchLastSection(True)
        self.status_table.setMaximumHeight(200)
        layout.addWidget(self.status_table)

        self.empty_label = QLabel("")
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet("color: gray;")
        layout.addWidget(self.empty_label)

        self.rebuild_btn = QPushButton("Create a status file from the runs already in this folder")
        self.rebuild_btn.clicked.connect(self._rebuild_status_from_disk)
        self.rebuild_btn.setVisible(False)
        layout.addWidget(self.rebuild_btn)

        cleanup_note = QLabel(
            "Every ACH group's flow field/UV-off control run is kept in a temporary folder here "
            "(named _base_ACH<value> / _control_ACH<value> / _phase1_ACH<value>) so a later sweep "
            "in this same folder can reuse it instead of re-meshing. Safe to delete any time - "
            "they hold no results of their own, only reusable working copies; your actual Z/ACH "
            "simulation results are never touched by this.")
        cleanup_note.setWordWrap(True)
        cleanup_note.setStyleSheet("color: gray;")
        layout.addWidget(cleanup_note)
        self.cleanup_btn = QPushButton("Delete temporary flow-field folders...")
        self.cleanup_btn.clicked.connect(self._cleanup_scratch_dirs)
        layout.addWidget(self.cleanup_btn)

        action_row = QHBoxLayout()
        self.uv_action_btn = QPushButton("Apply different UV design to same room")
        self.uv_action_btn.setCheckable(True)
        self.uv_action_btn.clicked.connect(lambda: self._select_action("uv"))
        action_row.addWidget(self.uv_action_btn)
        self.extend_action_btn = QPushButton("Extend specific run(s)")
        self.extend_action_btn.setCheckable(True)
        self.extend_action_btn.clicked.connect(lambda: self._select_action("extend"))
        action_row.addWidget(self.extend_action_btn)
        self.sweep_action_btn = QPushButton("Add more Z/ACH sweeps")
        self.sweep_action_btn.setCheckable(True)
        self.sweep_action_btn.clicked.connect(lambda: self._select_action("sweep"))
        action_row.addWidget(self.sweep_action_btn)
        layout.addLayout(action_row)

        self.uv_group = QGroupBox()
        uv_layout = QFormLayout(self.uv_group)
        uv_path_row = QHBoxLayout()
        self.uv_guv_edit = QLineEdit()
        uv_path_row.addWidget(self.uv_guv_edit, 1)
        uv_browse_btn = QPushButton("Browse...")
        uv_browse_btn.clicked.connect(self._browse_uv_guv)
        uv_path_row.addWidget(uv_browse_btn)
        uv_layout.addRow(".guv file", uv_path_row)
        self.uv_z_edit = QLineEdit()
        self.uv_z_edit.setPlaceholderText("e.g. 2, 6")
        uv_layout.addRow("Z values (comma-separated)", self.uv_z_edit)
        uv_z_note = QLabel("Prefilled with this project's own Z values - edit the list to re-evaluate "
                            "different ones under the new design instead.")
        uv_z_note.setWordWrap(True)
        uv_z_note.setStyleSheet("color: gray;")
        uv_layout.addRow(uv_z_note)
        uv_note = QLabel("Every ACH already swept in this project will be re-evaluated under the new "
                          "design - any whose flow settings haven't changed reuse the existing flow "
                          "field/control run instead of re-solving them.")
        uv_note.setWordWrap(True)
        uv_note.setStyleSheet("color: gray;")
        uv_layout.addRow(uv_note)
        self.uv_group.setVisible(False)
        layout.addWidget(self.uv_group)

        self.extend_group = QGroupBox()
        extend_layout = QVBoxLayout(self.extend_group)
        self.combo_list = QListWidget()
        extend_layout.addWidget(self.combo_list)
        duration_row = QFormLayout()
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 10_000_000)
        self.duration_spin.setValue(600)
        duration_row.addRow("New end time (s) - decay only in this version", self.duration_spin)
        extend_layout.addLayout(duration_row)
        extend_note = QLabel("Steady-state combinations aren't extendable from this dialog yet - use "
                              "the per-run resume flow (Run Simulations tab) for those.")
        extend_note.setWordWrap(True)
        extend_note.setStyleSheet("color: gray;")
        extend_layout.addWidget(extend_note)
        self.extend_group.setVisible(False)
        layout.addWidget(self.extend_group)

        self.sweep_group = QGroupBox()
        sweep_layout = QFormLayout(self.sweep_group)
        self.sweep_z_edit = QLineEdit()
        sweep_layout.addRow("Z values (comma-separated)", self.sweep_z_edit)
        self.sweep_ach_edit = QLineEdit()
        sweep_layout.addRow("ACH values (comma-separated)", self.sweep_ach_edit)
        sweep_note = QLabel("Already-completed combinations are skipped automatically.")
        sweep_note.setStyleSheet("color: gray;")
        sweep_layout.addRow(sweep_note)
        self.sweep_group.setVisible(False)
        layout.addWidget(self.sweep_group)

        self.msg_label = QLabel("")
        self.msg_label.setStyleSheet("color: red;")
        self.msg_label.setWordWrap(True)
        layout.addWidget(self.msg_label)

        buttons = QDialogButtonBox()
        self.run_btn = buttons.addButton("Run new Sweep(s)", QDialogButtonBox.AcceptRole)
        self.run_btn.clicked.connect(self._run_action)
        cancel_btn = buttons.addButton(QDialogButtonBox.Cancel)
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)

        if default_project_dir:
            self._load_folder()

    # -- load/refresh --

    def _browse_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Choose a project folder", self.dir_edit.text() or "")
        if path:
            self.dir_edit.setText(path.replace("/", "\\"))

    def _browse_uv_guv(self):
        start_dir = str(Path(self.uv_guv_edit.text()).parent) if self.uv_guv_edit.text() else ""
        path, _ = QFileDialog.getOpenFileName(self, "Choose a .guv file", start_dir, "GUV files (*.guv)")
        if path:
            self.uv_guv_edit.setText(path.replace("/", "\\"))

    def _load_folder(self):
        project_dir = self.dir_edit.text().strip()
        if not project_dir:
            self.msg_label.setText("Enter a project folder first.")
            return
        self._refresh(project_dir)

    def _refresh(self, project_dir):
        status, settings, room, guv_path, settings_path, error = load_extend_status(project_dir)
        self._project_dir = project_dir
        self._project_name = Path(project_dir).name
        self._status = status
        self._settings = settings
        self._room = room
        self._guv_path = guv_path
        self._settings_path = settings_path
        self.msg_label.setText(error or "")
        self._render_status_table()

        combos = (status or {}).get("combos") or {}
        no_status_at_all = not combos and not (status or {}).get("guv_path")
        self.rebuild_btn.setVisible(bool(project_dir) and no_status_at_all)

        z_values = sorted({c["z"] for c in combos.values() if "z" in c})
        ach_values = sorted({c["ach"] for c in combos.values() if "ach" in c})
        self.sweep_z_edit.setText(", ".join(f"{v:g}" for v in z_values))
        self.sweep_ach_edit.setText(", ".join(f"{v:g}" for v in ach_values))
        # Prefilled (not left blank) so the user sees exactly which Z
        # values are about to be re-evaluated rather than having to
        # remember/guess what an empty field defaults to - avoids the
        # confusion a blank-means-"all of them" convention caused.
        self.uv_z_edit.setText(", ".join(f"{v:g}" for v in z_values))
        if guv_path:
            self.uv_guv_edit.setText(guv_path)
        self._populate_combo_list(combos)

    def _render_status_table(self):
        combos = (self._status or {}).get("combos") or {}
        self.status_table.setRowCount(0)
        if not combos:
            self.empty_label.setText(
                "No recorded runs for this folder yet. If it's a brand new project directory, the "
                "3 actions below still work, there's just nothing to show a history of. If it "
                "already has real runs in it from before this feature existed, use \"Create a "
                "status file from the runs already in this folder\" below to rebuild one from them.")
            return
        self.empty_label.setText("")
        rows = sorted(combos.items(), key=lambda kv: (kv[1].get("ach", 0), kv[1].get("z", 0)))
        self.status_table.setRowCount(len(rows))
        for i, (key, c) in enumerate(rows):
            ach = c.get("ach")
            control_ok = phase1_ok = False
            if self._project_dir is not None and ach is not None:
                label = _ach_label_for_dirs(ach)
                control_ok = Path(f"{self._project_dir}/_control_ACH{label}").exists()
                phase1_ok = Path(f"{self._project_dir}/_phase1_ACH{label}").exists()
            values = [
                f"{c.get('z')}", f"{c.get('ach')}", c.get("status", ""),
                "✓" if c.get("flow_fingerprint") else "", "✓" if c.get("uv_fingerprint") else "",
                "✓" if control_ok else "", "✓" if phase1_ok else "",
            ]
            for col, text in enumerate(values):
                self.status_table.setItem(i, col, QTableWidgetItem(text))

    def _populate_combo_list(self, combos):
        self.combo_list.clear()
        for key, c in sorted(combos.items()):
            item = QListWidgetItem(f"Z={c.get('z')} / ACH={c.get('ach')} ({c.get('status')})")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, key)
            self.combo_list.addItem(item)

    def _rebuild_status_from_disk(self):
        project_dir = self.dir_edit.text().strip()
        if not project_dir:
            self.msg_label.setText("Enter a project folder first.")
            return
        guv_path = scenario_runs.find_first_guv_path_on_disk(project_dir)
        if not guv_path:
            self.msg_label.setText(
                "No existing run folders (with their own run_settings.json) were found in this "
                "location to rebuild a status file from.")
            return
        try:
            project = Project.load(guv_path)
            room = next(iter(project.rooms.values()))
        except Exception as e:
            self.msg_label.setText(f"Found run folders, but failed to load {guv_path}: {e}")
            return
        project_name = Path(project_dir).name
        n_found = scenario_runs.rebuild_project_status_from_disk(project_dir, project_name, room)
        self._refresh(project_dir)
        prefix = f"Rebuilt a status file from {n_found} existing run folder(s)."
        self.msg_label.setText(f"{prefix} {self.msg_label.text()}".strip())

    def _cleanup_scratch_dirs(self):
        if not self._project_dir or not self._status:
            self.msg_label.setText("Load a project folder first.")
            return
        existing = scratch_dirs_for_status(self._project_dir, self._status)
        if not existing:
            self.msg_label.setText("No temporary flow-field folders found for this project - nothing to delete.")
            return
        names = ", ".join(Path(d).name for d in existing)
        reply = QMessageBox.question(
            self, "Delete temporary flow-field folders?",
            f"This will permanently delete {len(existing)} folder(s) in {self._project_dir}:\n\n{names}\n\n"
            f"These hold only reusable working copies of the flow field, never simulation results - "
            f"your Z/ACH combination folders and their results.json are NOT affected. A future sweep "
            f"will simply rebuild whatever it needs. Delete these now?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        quoted = " ".join(f'"{wsl_path(d)}"' for d in existing)
        run_wsl_or_raise(f"rm -rf {quoted}", wsl_path(self._project_dir), "deleting temporary flow-field folders")
        clear_ach_bases(self._project_dir, self._project_name)
        self._refresh(self._project_dir)
        self.msg_label.setText(f"Deleted {len(existing)} temporary flow-field folder(s).")

    # -- action selection --

    def _select_action(self, action):
        self._action = action
        self.uv_action_btn.setChecked(action == "uv")
        self.extend_action_btn.setChecked(action == "extend")
        self.sweep_action_btn.setChecked(action == "sweep")
        self.uv_group.setVisible(action == "uv")
        self.extend_group.setVisible(action == "extend")
        self.sweep_group.setVisible(action == "sweep")

    # -- run dispatch --

    def _run_action(self):
        if not self._project_dir or self._settings is None or self._room is None:
            self.msg_label.setText("Load a valid project folder first.")
            return
        if self.run_tab.state.status == "running" or self.run_tab.sweep_state.status == "running":
            self.msg_label.setText("A simulation is already running - stop it first.")
            return

        combos = (self._status or {}).get("combos") or {}
        adv = merge_project_openfoam_settings(self._settings, load_advanced_settings())

        if self._action == "uv":
            guv_path = self.uv_guv_edit.text().strip()
            if not guv_path:
                self.msg_label.setText("Pick a .guv file first.")
                return
            try:
                uv_z_values = helpers.parse_number_list(self.uv_z_edit.text())
            except ValueError as e:
                self.msg_label.setText(f"Can't parse Z value list: {e}")
                return
            # Blank field - use every Z value already recorded for this
            # project (see the note in the dialog itself) rather than
            # requiring the user to retype a list they already swept once.
            z_values = (uv_z_values or sorted({c["z"] for c in combos.values() if "z" in c})
                        or [self._settings.get("z-value")])
            ach_values = sorted({c["ach"] for c in combos.values() if "ach" in c}) or [self._settings.get("ach")]
            self.run_tab.launch_sweep_from_extend(
                guv_path, self._settings_path, self._project_dir, self._room,
                dict(self._settings, **{"z-value": z_values[0]}), adv, z_values, ach_values)
            self.accept()
        elif self._action == "sweep":
            try:
                z_values = helpers.parse_number_list(self.sweep_z_edit.text())
                ach_values = helpers.parse_number_list(self.sweep_ach_edit.text())
            except ValueError as e:
                self.msg_label.setText(f"Can't parse Z/ACH list: {e}")
                return
            if not z_values or not ach_values:
                self.msg_label.setText("Enter at least one Z value and one ACH value.")
                return
            self.run_tab.launch_sweep_from_extend(
                self._guv_path, self._settings_path, self._project_dir, self._room,
                self._settings, adv, z_values, ach_values)
            self.accept()
        elif self._action == "extend":
            selected = [self.combo_list.item(i) for i in range(self.combo_list.count())
                        if self.combo_list.item(i).checkState() == Qt.Checked]
            if not selected:
                self.msg_label.setText("Pick at least one combination to extend.")
                return
            if self._settings.get("sim-type") != "decay":
                self.msg_label.setText("Extending is only supported for decay projects in this version.")
                return
            end_time = self.duration_spin.value()
            write_interval = max(1, self._settings.get("pimple-write-interval") or 1)
            case_dirs = []
            for item in selected:
                key = item.data(Qt.UserRole)
                combo = combos.get(key)
                if not combo:
                    continue
                z, ach = combo["z"], combo["ach"]
                case_dirs.append((f"{self._project_dir}/{scenario_runs._subdir_name(z, ach)}", z, ach))
            self.run_tab.launch_extend_from_extend(case_dirs, end_time, write_interval)
            self.accept()
        else:
            self.msg_label.setText("Choose one of the 3 actions above first.")
