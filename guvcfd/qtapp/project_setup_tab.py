"""Project Setup tab: load a .guv project, configure inlet/outlet/fan/
monitoring/simulation-type, and see a live 3D preview - the native
equivalent of guvcfd.app's Project Setup tab."""
import json
import sys
from pathlib import Path

from guv_calcs import Project
from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QSplitter, QTabWidget,
    QVBoxLayout, QWidget,
)

from ..app_settings import capture_openfoam_settings, load_advanced_settings, PROJECT_OPENFOAM_SETTINGS_KEYS
from ..visualization import WALL_POSITION_DIMS
from . import helpers
from .preview3d import Preview3D

WALLS = ["xMin", "xMax", "frontWall", "backWall", "floor", "ceiling"]

_GRID_SNAP_NOTE = ("Position and size are automatically snapped OUTWARD to the mesh grid (cell size, "
                    "0.1m by default) - the actual carved opening may end up up to one cell LARGER "
                    "than entered here, but never smaller.")


def _dspin(minimum=-1000.0, maximum=1000.0, step=0.05, decimals=3, value=0.0):
    w = QDoubleSpinBox()
    w.setRange(minimum, maximum)
    w.setSingleStep(step)
    w.setDecimals(decimals)
    w.setValue(value)
    return w


def _ispin(minimum=0, maximum=10 ** 9, value=0):
    w = QSpinBox()
    w.setRange(minimum, maximum)
    w.setValue(value)
    return w


def _add_row(form, label_text, widget, tooltip=None):
    """form.addRow(str, widget) only ever makes the WIDGET hoverable for a
    tooltip - the auto-generated label (the text a user actually reads and
    hovers over first) gets none, which is why tooltips looked broken:
    hovering the label itself (natural reading behavior) showed nothing.
    This builds an explicit QLabel and puts the tooltip on both.
    tooltip=None reuses whatever tooltip is already set on `widget` (many
    callers set one there directly before calling this), so callers don't
    have to repeat the text.
    """
    tooltip = tooltip or widget.toolTip() or None
    label = QLabel(label_text)
    if tooltip:
        label.setToolTip(tooltip)
        widget.setToolTip(tooltip)
    form.addRow(label, widget)
    return widget


_MAX_RECENT_PROJECTS = 10


class ProjectSetupTab(QWidget):
    project_loaded = Signal()  # emitted after a .guv is loaded, room/lamps ready
    recent_projects_changed = Signal()  # emitted whenever the recent-projects list changes

    def __init__(self, parent=None):
        super().__init__(parent)
        self.room = None
        self.guv_path = None
        # The currently open/saved .guvcfd project file (None if this
        # project has never been saved) - "Save Project" writes back here
        # without prompting; "Save Project As..." always prompts and
        # updates this. Distinct from guv_path, the raw room-design file a
        # .guvcfd project references (see save_project/load_guvcfd_project).
        self.settings_path = None
        # Explicit, in-session overrides for PROJECT_OPENFOAM_SETTINGS_KEYS
        # (mesh-cell-size, max-co, ...) applied via the Settings dialog
        # while THIS project is loaded - takes priority in gather_settings()
        # over both the on-disk settings_path re-read (below) and the
        # global default. Without this, a deliberate Settings change had
        # no way to survive ANY save (same file or Save As) - confirmed as
        # a real incident: a mesh-cell-size change made via Settings while
        # an old project was loaded silently never took effect, even after
        # "Save As" to a brand new file name (settings_path still pointed
        # at the OLD file at the moment gather_settings() re-read it - see
        # that method's own comment). Cleared on a fresh project load
        # (see load_project/load_guvcfd_project) - overrides are explicitly
        # per-loaded-project, not carried across to a different one.
        self._openfoam_overrides = {}
        self.fields = {}  # field id -> widget
        # Persisted across sessions (Windows registry, HKCU\Software\GUV-CFD\
        # qtapp) - last-used folder for file dialogs, and up to
        # _MAX_RECENT_PROJECTS most-recently-opened/saved project paths for
        # File > Open Recent.
        self.qsettings = QSettings("GUV-CFD", "qtapp")

        splitter = QSplitter(Qt.Horizontal)
        outer = QVBoxLayout(self)
        outer.addWidget(splitter)

        left = QScrollArea()
        left.setWidgetResizable(True)
        left_inner = QWidget()
        left.setWidget(left_inner)
        self.form_layout = QVBoxLayout(left_inner)
        splitter.addWidget(left)

        self.preview = Preview3D()
        splitter.addWidget(self.preview)
        splitter.setSizes([420, 640])

        self._build_project_group()
        self._build_case_dir_group()
        # Simulation type/timing/budget fields exist (registered in
        # self.fields, so gather_settings()/apply_settings() see them) but
        # are NOT part of this tab's visible layout - they live in the
        # Simulation Settings dialog instead, opened from the Run
        # Simulations tab (matches guvcfd.app: this used to be a Project
        # Setup card, moved to a dialog since it's run tuning, not room/
        # problem geometry - see _build_sim_type_fields).
        self._build_sim_type_fields()
        self._build_simulation_settings_dialog()
        self._build_opening_group("inlet", "Inlet")
        self._build_second_opening_group("inlet2", "2nd Inlet", "ceiling", is_inlet=True)
        self._build_opening_group("outlet", "Outlet")
        self._build_second_opening_group("outlet2", "2nd Outlet", "floor", is_inlet=False)
        self._build_fan_group()
        self._build_source_group()
        self._build_monitoring_group()
        self.form_layout.addStretch(1)

        for w in self.fields.values():
            self._connect_preview_refresh(w)

    # -- field plumbing --

    def _register(self, field_id, widget):
        self.fields[field_id] = widget
        return widget

    def _connect_preview_refresh(self, widget):
        if isinstance(widget, (QDoubleSpinBox, QSpinBox)):
            widget.valueChanged.connect(self.refresh_preview)
        elif isinstance(widget, QComboBox):
            widget.currentTextChanged.connect(self.refresh_preview)
        elif isinstance(widget, QCheckBox):
            widget.toggled.connect(self.refresh_preview)

    def _project_label_text(self):
        """Builds project_label's text from current state - always shows
        BOTH which .guvcfd project file is loaded (or that there isn't
        one) and which .guv room/lamp design it references, since neither
        alone answers "which file am I actually working with". Confirmed
        as a real, costly gap: with no project-file indicator anywhere,
        a mesh-size change made via Settings while an OLD .guvcfd was
        still loaded, then saved under a NEW name, was impossible to
        catch by eye - the display gave no way to notice which file was
        actually in play.
        """
        if self.room is None:
            return "No project loaded"
        project_part = Path(self.settings_path).name if self.settings_path else "(unsaved project)"
        return (f"{project_part} — {Path(self.guv_path).name if self.guv_path else '?'} — "
                f"room {self.room.x:.2f}×{self.room.y:.2f}×{self.room.z:.2f} {self.room.units}, "
                f"{len(self.room.lamps)} lamp(s)")

    def get_value(self, field_id):
        w = self.fields.get(field_id)
        if w is None:
            return None
        if isinstance(w, (QDoubleSpinBox, QSpinBox)):
            return w.value()
        if isinstance(w, QComboBox):
            return w.currentText()
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QLineEdit):
            return w.text()
        return None

    def set_value(self, field_id, value):
        """Restore a single field's value (e.g. loading a saved .guvcfd
        project) - a no-op for an unknown field id or a None value (an
        older/hand-edited .guvcfd missing a field just leaves that widget
        at whatever it already was, same as guvcfd.app's own load-project
        fallback-to-current-default behavior)."""
        w = self.fields.get(field_id)
        if w is None or value is None:
            return
        if isinstance(w, (QDoubleSpinBox, QSpinBox)):
            try:
                w.setValue(type(w.value())(value))
            except (TypeError, ValueError):
                pass
        elif isinstance(w, QComboBox):
            idx = w.findText(str(value))
            if idx >= 0:
                w.setCurrentIndex(idx)
        elif isinstance(w, QCheckBox):
            w.setChecked(bool(value))
        elif isinstance(w, QLineEdit):
            w.setText(str(value))

    def gather_settings(self):
        settings = {fid: self.get_value(fid) for fid in self.fields}
        settings["case-dir"] = self.case_dir_edit.text()
        settings["sim-type"] = "decay" if self.sim_type_combo.currentText().startswith("Decay") else "steady_state"
        # PROJECT_OPENFOAM_SETTINGS_KEYS (max-co, cell size, etc.) have no
        # registered widget (self.fields) - without the disk re-read
        # below, a value captured/hand-edited into settings_path on disk
        # would never be seen here, and the next save_project() would
        # silently overwrite it back to the global default. See app.py's
        # _collect_settings (the Dash equivalent of this method) for the
        # same fix.
        #
        # self._openfoam_overrides (an explicit, in-session Settings-
        # dialog change - see its own docstring) is applied AFTER the
        # disk re-read, so it always wins - this is what makes a
        # deliberate Settings change actually stick, on Save AND Save As,
        # instead of the disk re-read silently clobbering it back (the
        # confirmed bug this closes: Save As re-reads the OLD
        # settings_path, since it's only updated to the NEW path afterward
        # - see save_project - so without this override taking priority,
        # a fresh Settings change never survives even a save to a brand
        # new file name).
        if self.settings_path:
            try:
                with open(self.settings_path) as f:
                    raw = json.load(f)
                for key in PROJECT_OPENFOAM_SETTINGS_KEYS:
                    if key in raw:
                        settings[key] = raw[key]
            except (OSError, json.JSONDecodeError):
                pass
        settings.update(self._openfoam_overrides)
        return settings

    def apply_project_openfoam_overrides(self, values):
        """Record explicit PROJECT_OPENFOAM_SETTINGS_KEYS changes made via
        the Settings dialog while this project is loaded - see
        self._openfoam_overrides's own docstring for why this is needed
        at all. `values` may contain other, non-project keys too (the
        Settings dialog's own global fields) - only known
        PROJECT_OPENFOAM_SETTINGS_KEYS are recorded here.
        """
        for key in PROJECT_OPENFOAM_SETTINGS_KEYS:
            if key in values:
                self._openfoam_overrides[key] = values[key]

    def _clamp_position_fields_to_room(self):
        """Caps every position field's max to the actual loaded room
        dimension it maps to - mirrors app._register_position_field/
        _register_opening_wall_axes. A no-op until a project is loaded
        (self.room is None). Confirmed 2026-08-10 Qt had no equivalent at
        all - a user could type e.g. Y=40 into a 4m-wide room with no
        clamp or warning. Opening fields (inlet/outlet/inlet2/outlet2)
        need re-clamping whenever their own Wall dropdown changes, since
        Position 1/2 map to different room dimensions depending on which
        wall the opening is mounted on (see WALL_POSITION_DIMS) - this is
        connected to every such dropdown's currentTextChanged, not just
        called once at load time.
        """
        if self.room is None:
            return
        room = self.room
        for prefix in ("fan", "inject"):
            self.fields[f"{prefix}-x-input"].setMaximum(room.x)
            self.fields[f"{prefix}-y-input"].setMaximum(room.y)
            self.fields[f"{prefix}-z-input"].setMaximum(room.z)
        for i in helpers.MONITOR_POINT_IDS:
            self.fields[f"monitor{i}-x-input"].setMaximum(room.x)
            self.fields[f"monitor{i}-y-input"].setMaximum(room.y)
            self.fields[f"monitor{i}-z-input"].setMaximum(room.z)
        for prefix in ("inlet", "outlet", "inlet2", "outlet2"):
            wall = self.fields[f"{prefix}-wall"].currentText()
            dim1, dim2 = WALL_POSITION_DIMS.get(wall, ("y", "z"))
            self.fields[f"{prefix}-y-input"].setMaximum(getattr(room, dim1))
            self.fields[f"{prefix}-z-input"].setMaximum(getattr(room, dim2))

    def apply_settings(self, settings):
        """Inverse of gather_settings() - restores every field from a saved
        settings dict (a .guvcfd project file's contents, or run_settings.json)."""
        for fid, value in settings.items():
            self.set_value(fid, value)
        if "case-dir" in settings and settings["case-dir"]:
            self.case_dir_edit.setText(settings["case-dir"])
        if settings.get("sim-type") == "steady_state":
            self.sim_type_combo.setCurrentIndex(1)
        elif settings.get("sim-type") == "decay":
            self.sim_type_combo.setCurrentIndex(0)
        self.refresh_preview()

    # -- UI groups --

    def _build_project_group(self):
        box = QGroupBox("Project")
        box.setToolTip(
            "\"Load .guv file...\" starts a brand-new GUV-CFD project from a raw room/lamp design "
            "file (no inlet/outlet/fan settings yet - room defaults only). To reopen a project "
            "you've already configured and saved (a .guvcfd file, which remembers those settings "
            "too), use File > Open Project instead.")
        layout = QVBoxLayout(box)
        row = QHBoxLayout()
        btn = QPushButton("Load .guv file...")
        btn.setToolTip("Start a new project from a raw .guv room/lamp design file (no saved "
                        "inlet/outlet/fan settings - use File > Open Project for those).")
        btn.clicked.connect(self.load_project_dialog)
        row.addWidget(btn)
        self.project_label = QLabel("No project loaded")
        row.addWidget(self.project_label, 1)
        layout.addLayout(row)
        self.form_layout.addWidget(box)

    def _build_case_dir_group(self):
        box = QGroupBox("OpenFOAM Project Directory")
        layout = QHBoxLayout(box)
        self.case_dir_edit = QLineEdit()
        layout.addWidget(self.case_dir_edit, 1)
        btn = QPushButton("Browse...")
        btn.clicked.connect(self._browse_case_dir)
        layout.addWidget(btn)
        self.form_layout.addWidget(box)

    def _browse_case_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Choose (or create) an OpenFOAM project directory")
        if d:
            self.case_dir_edit.setText(d)

    def _build_sim_type_fields(self):
        self.sim_type_combo = QComboBox()
        self.sim_type_combo.addItems(["Decay (one-time contamination event)",
                                       "Steady-state (continuous source)"])
        self.sim_type_combo.currentTextChanged.connect(self.refresh_preview)

        ach_field = self._register("ach", _dspin(0, 100, 0.5, 3, 3.0))
        ach_field.setToolTip(
            "Target air changes per hour. Setting this to 0 seals the room (inlet/outlet closed "
            "off as walls) - only the mixing fan, if enabled, moves air. Decay mode only; needs the "
            "fan enabled.")
        z_field = self._register("z-value", _dspin(0, 100, 0.5, 3, 2.0))
        z_field.setToolTip("UV susceptibility constant for the target organism - higher means the "
                            "same UV dose inactivates it faster.")
        pimple_end = self._register("pimple-end-time", _ispin(1, 100000, 120))
        pimple_end.setToolTip("Starting value only - the actual run duration is computed adaptively "
                               "once the well-mixed eACH estimate is known, and overrides this.")
        self._register("pimple-write-interval", _ispin(1, 10000, 10))
        mech_ach_only = self._register("mech-ach-only", QCheckBox("Run mechanical ACH only (no UV)"))
        mech_ach_only.setToolTip(
            "Skips the fluence/UV-inactivation pipeline entirely and measures just the real, "
            "CFD-delivered ventilation air-change rate - for a pure ventilation study, "
            "independent of whether the project has lamps. Needs ACH>0 (real ventilation to "
            "measure). Decay mode only.")
        self._register("phase1-iterations", _ispin(1, 200000, 4000))
        self._register("phase2-iterations", _ispin(1, 200000, 2000))
        t_ss_window = self._register("t-ss-window-frac", _dspin(0.01, 1.0, 0.01, 2, 0.15))
        t_ss_window.setToolTip(
            "Room-wide T and every monitoring point report a trailing-window mean/CV over this "
            "fraction of the live per-iteration samples, instead of a single last-sample read. "
            "0.15 = last 15% of samples.")
        deltat_enable = self._register("deltat-scaling-enabled", QCheckBox("Scale time steps automatically"))
        deltat_enable.setChecked(True)
        deltat_enable.setToolTip(
            "Lets the iteration budget above cover enough real residence time to reach a reliable "
            "steady state, by scaling OpenFOAM's own pseudo time step instead of running more "
            "iterations - simpleFoam's U/p/k/omega solve has no time-derivative term at all, so "
            "this doesn't touch flow-field stability. On by default; a case that already converges "
            "fine at deltaT=1 (typically higher-ACH, short residence time) is unaffected.")
        deltat_frac = self._register("deltat-effective-fraction", _dspin(0.01, 2.0, 0.05, 2, 0.7))
        deltat_frac.setToolTip(
            "\"Effective\" = real, measured ventilation/UV removal typically runs below the nominal "
            "value used to size this room. Lower-ACH rooms need a proportionally larger time-step "
            "scale to reach the same real-time coverage within the iteration budget.")
        deltat_target = self._register("deltat-target-fraction", _dspin(0.5, 0.9999, 0.005, 4, 0.995))
        deltat_target.setToolTip(
            "How close to true steady state the scaled deltaT targets within the iteration budget "
            "- 0.995 ~= 5.3 residence times.")

    def _build_simulation_settings_dialog(self):
        """The fields _build_sim_type_fields() created, laid out in a
        dialog (opened from the Run Simulations tab's "Simulation
        Settings..." button, see run_tab.py) rather than inline here."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Simulation Settings")
        dlg.resize(520, 560)
        layout = QVBoxLayout(dlg)

        top_form = QFormLayout()
        top_form.addRow("Simulation type", self.sim_type_combo)
        _add_row(top_form, "Ventilation ACH (0 = sealed room, decay only)", self.fields["ach"])
        _add_row(top_form, "UV inactivation constant Z (cm²/mJ)", self.fields["z-value"])
        layout.addLayout(top_form)

        decay_box = QGroupBox("Decay solver run settings")
        decay_form = QFormLayout(decay_box)
        _add_row(decay_form, "Suggested duration (s)", self.fields["pimple-end-time"])
        decay_form.addRow("Write interval (s)", self.fields["pimple-write-interval"])
        decay_form.addRow(self.fields["mech-ach-only"])
        layout.addWidget(decay_box)

        ss_box = QGroupBox("Steady-state run budget")
        ss_form = QFormLayout(ss_box)
        ss_form.addRow("Phase 1 iterations (no UV)", self.fields["phase1-iterations"])
        ss_form.addRow("Phase 2 iterations (UV on)", self.fields["phase2-iterations"])
        _add_row(ss_form, "T_ss moving-average window (fraction)", self.fields["t-ss-window-frac"])
        layout.addWidget(ss_box)

        deltat_box = QGroupBox("Residence-time-scaled deltaT (steady-state)")
        deltat_layout = QVBoxLayout(deltat_box)
        note = QLabel("Speeds up low-ACH steady-state runs for free - see tooltip on the checkbox below.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        deltat_layout.addWidget(note)
        deltat_layout.addWidget(self.fields["deltat-scaling-enabled"])
        deltat_form = QFormLayout()
        _add_row(deltat_form, "Expected ACH/eACH effectiveness (x)", self.fields["deltat-effective-fraction"])
        _add_row(deltat_form, "Target fraction of steady state", self.fields["deltat-target-fraction"])
        deltat_layout.addLayout(deltat_form)
        layout.addWidget(deltat_box)

        layout.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        buttons.button(QDialogButtonBox.Close).clicked.connect(dlg.accept)
        layout.addWidget(buttons)

        self.simulation_settings_dialog = dlg

    def _build_opening_group(self, prefix, label):
        box = QGroupBox(label)
        form = QFormLayout(box)
        show = self._register(f"{prefix}-show", QCheckBox("Show in preview"))
        show.setChecked(True)
        form.addRow(show)
        wall = QComboBox()
        wall.addItems(WALLS)
        wall.setCurrentText("xMin" if prefix == "inlet" else "xMax")
        form.addRow("Wall", self._register(f"{prefix}-wall", wall))
        form.addRow("Position 1 (m)", self._register(f"{prefix}-y-input", _dspin(0, 50, 0.05, 3, 1.5)))
        form.addRow("Position 2 (m)", self._register(f"{prefix}-z-input", _dspin(0, 50, 0.05, 3, 1.5)))
        size_w = self._register(f"{prefix}-size-w", _dspin(0.05, 2.0, 0.05, 3, 0.4))
        size_h = self._register(f"{prefix}-size-h", _dspin(0.05, 2.0, 0.05, 3, 0.4))
        _add_row(form, "Width (m)", size_w, _GRID_SNAP_NOTE)
        _add_row(form, "Height (m)", size_h, _GRID_SNAP_NOTE)
        wall.currentTextChanged.connect(self._clamp_position_fields_to_room)
        if prefix.startswith("inlet"):
            diff = QComboBox()
            diff.addItems(["direct", "ceiling"])
            _add_row(form, "Diffuser type", self._register(f"{prefix}-diffuser-type", diff),
                      "Direct jet: a single beam straight into the room. Surface-attached (ceiling): "
                      "spreads radially along the wall/ceiling like a real diffuser - validated for "
                      "round/square ceiling, vortex, and grille types. Currently opt-in while a "
                      "numerical instability with certain opening sizes/geometries is being root-caused.")
        self.form_layout.addWidget(box)
        return box

    def _build_second_opening_group(self, prefix, label, default_wall, is_inlet):
        """A 2nd inlet/outlet, off by default - mirrors the primary
        opening's own fields (see _build_opening_group), gated behind an
        "Enable 2nd ..." checkbox (matches guvcfd.app's
        _second_opening_controls)."""
        outer = QGroupBox(label)
        outer_layout = QVBoxLayout(outer)
        enable = self._register(f"{prefix}-enable", QCheckBox(f"Enable {label.lower()}"))
        outer_layout.addWidget(enable)

        inner = QGroupBox()
        inner.setFlat(True)
        form = QFormLayout(inner)
        wall = QComboBox()
        wall.addItems(WALLS)
        wall.setCurrentText(default_wall)
        form.addRow("Wall", self._register(f"{prefix}-wall", wall))
        form.addRow("Position 1 (m)", self._register(f"{prefix}-y-input", _dspin(0, 50, 0.05, 3, 1.5)))
        form.addRow("Position 2 (m)", self._register(f"{prefix}-z-input", _dspin(0, 50, 0.05, 3, 1.5)))
        size_w = self._register(f"{prefix}-size-w", _dspin(0.05, 2.0, 0.05, 3, 0.3))
        size_h = self._register(f"{prefix}-size-h", _dspin(0.05, 2.0, 0.05, 3, 0.3))
        _add_row(form, "Width (m)", size_w, _GRID_SNAP_NOTE)
        _add_row(form, "Height (m)", size_h, _GRID_SNAP_NOTE)
        wall.currentTextChanged.connect(self._clamp_position_fields_to_room)
        if is_inlet:
            diff = QComboBox()
            diff.addItems(["direct", "ceiling"])
            form.addRow("Diffuser type", self._register(f"{prefix}-diffuser-type", diff))
        outer_layout.addWidget(inner)
        inner.setVisible(enable.isChecked())
        enable.toggled.connect(inner.setVisible)
        self.form_layout.addWidget(outer)
        return outer

    def _build_fan_group(self):
        box = QGroupBox("Mixing Fan")
        form = QFormLayout(box)
        enable = self._register("fan-enable", QCheckBox("Enable fan"))
        form.addRow(enable)
        form.addRow("Speed (m/s)", self._register("fan-speed", _dspin(0, 1.5, 0.05, 3, 0.5)))
        direction = QComboBox()
        direction.addItems(["down", "up"])
        _add_row(form, "Direction", self._register("fan-direction", direction),
                  "Which way the fan pushes air along its own axis.")
        form.addRow("Radius (m)", self._register("fan-radius", _dspin(0.1, 1.5, 0.05, 3, 0.6)))
        form.addRow("Thickness (m)", self._register("fan-thickness", _dspin(0.05, 1.0, 0.05, 3, 0.2)))
        form.addRow("X position (m)", self._register("fan-x-input", _dspin(0, 50, 0.05, 3, 2.0)))
        form.addRow("Y position (m)", self._register("fan-y-input", _dspin(0, 50, 0.05, 3, 1.5)))
        form.addRow("Z position (m)", self._register("fan-z-input", _dspin(0, 20, 0.05, 3, 2.2)))
        self.form_layout.addWidget(box)

    def _build_source_group(self):
        box = QGroupBox("Contaminant Source Geometry (steady-state mode)")
        layout = QVBoxLayout(box)
        note = QLabel(_GRID_SNAP_NOTE)
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)
        form = QFormLayout()
        form.addRow("X position (m)", self._register("inject-x-input", _dspin(0, 50, 0.05, 3, 2.0)))
        form.addRow("Y position (m)", self._register("inject-y-input", _dspin(0, 50, 0.05, 3, 1.5)))
        form.addRow("Z position (m)", self._register("inject-z-input", _dspin(0, 20, 0.05, 3, 1.5)))
        size_field = self._register("source-zone-size", _dspin(0.05, 2.0, 0.05, 3, 0.3))
        _add_row(form, "Source zone size (m)", size_field,
                  "Side length of the cube-shaped cellZone the contaminant source injects into. "
                  "Larger zones dilute the injection over more cells; smaller zones concentrate it "
                  "into fewer, higher-rate cells. Only used for steady-state runs.")
        layout.addLayout(form)
        self.form_layout.addWidget(box)

    def _build_monitoring_group(self):
        box = QGroupBox("Monitoring Points (optional)")
        layout = QVBoxLayout(box)
        enable = self._register("monitoring-enable", QCheckBox("Enable monitoring points"))
        layout.addWidget(enable)
        for i in (1, 2, 3):
            form = QFormLayout()
            pt_enable = self._register(f"monitor{i}-enable", QCheckBox(f"Point {i} enabled"))
            form.addRow(pt_enable)
            form.addRow("Name", self._register(f"monitor{i}-name", QLineEdit(f"Point {i}")))
            form.addRow("X (m)", self._register(f"monitor{i}-x-input", _dspin(0, 50, 0.05, 3, 2.0)))
            form.addRow("Y (m)", self._register(f"monitor{i}-y-input", _dspin(0, 50, 0.05, 3, 1.5)))
            form.addRow("Z (m)", self._register(f"monitor{i}-z-input", _dspin(0, 20, 0.05, 3, 1.5)))
            cells_field = self._register(f"monitor{i}-cells", _ispin(1, 20, 4))
            _add_row(form, "Cells per side", cells_field,
                      "Size (in mesh cells) of the small cube around this point that its "
                      "concentration reading is averaged over.")
            layout.addLayout(form)
        self.form_layout.addWidget(box)

    # -- last-used-directory / recent-projects memory (QSettings, persists
    # across sessions) --

    def _last_project_dir(self):
        return self.qsettings.value("last_project_dir", "")

    def _remember_project_path(self, path):
        """Called after every successful open/save - updates BOTH the
        last-used directory (so the next file dialog starts there) and the
        recent-projects list (File > Open Recent, most-recent-first, capped
        at _MAX_RECENT_PROJECTS, deduplicated)."""
        self.qsettings.setValue("last_project_dir", str(Path(path).parent))
        recents = [p for p in self.recent_projects() if p != path]
        recents.insert(0, path)
        self.qsettings.setValue("recent_projects", json.dumps(recents[:_MAX_RECENT_PROJECTS]))
        self.recent_projects_changed.emit()

    def recent_projects(self):
        raw = self.qsettings.value("recent_projects", "")
        if not raw:
            return []
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return []

    def open_recent_project(self, path):
        if not Path(path).exists():
            QMessageBox.warning(self, "File not found", f"{path}\n\nno longer exists - removing it "
                                 "from the recent-projects list.")
            recents = [p for p in self.recent_projects() if p != path]
            self.qsettings.setValue("recent_projects", json.dumps(recents))
            self.recent_projects_changed.emit()
            return
        if path.lower().endswith(".guvcfd"):
            self.load_guvcfd_project(path)
        else:
            self.load_project(path)

    # -- project loading --

    def load_project_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open .guv Project", self._last_project_dir(), "GUV project files (*.guv)")
        if path:
            self.load_project(path)

    def load_project(self, path):
        try:
            project = Project.load(path)
            room = next(iter(project.rooms.values()))
        except Exception as e:
            QMessageBox.critical(self, "Failed to load project", str(e))
            return
        self.guv_path = path
        self.room = room
        self._clamp_position_fields_to_room()
        self.settings_path = None  # a bare .guv load starts a fresh, never-saved project
        self._openfoam_overrides = {}  # overrides are per-loaded-project, not carried over
        self.project_label.setText(self._project_label_text())
        if not self.case_dir_edit.text():
            default_dir = helpers.compute_default_run_dir()
            self.case_dir_edit.setText(helpers.fresh_case_dir(path, default_dir))
        self._remember_project_path(path)
        self.refresh_preview()
        self.project_loaded.emit()

    def open_guvcfd_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open GUV-CFD Project", self._last_project_dir(),
            "GUV-CFD project files (*.guvcfd);;All files (*)")
        if path:
            self.load_guvcfd_project(path)

    def load_guvcfd_project(self, path):
        """Open a saved .guvcfd project - a JSON file recording guv_path
        plus every field this tab exposes (see save_project) - restoring
        both the referenced room AND all previously-configured inlet/
        outlet/fan/etc. settings, unlike load_project() (a bare .guv,
        room defaults only)."""
        try:
            with open(path) as f:
                settings = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Failed to open project", str(e))
            return
        guv_path = settings.get("guv_path")
        if guv_path:
            try:
                project = Project.load(guv_path)
                room = next(iter(project.rooms.values()))
            except Exception as e:
                QMessageBox.critical(self, "Failed to load referenced .guv file", str(e))
                return
            self.guv_path = guv_path
            self.room = room
        self.settings_path = path
        self._openfoam_overrides = {}  # overrides are per-loaded-project, not carried over
        self.project_label.setText(self._project_label_text())
        self._clamp_position_fields_to_room()
        self.apply_settings(settings)
        self._clamp_position_fields_to_room()
        self._remember_project_path(path)
        self.project_loaded.emit()

    def save_project_dialog(self, force_new_path=False):
        path = None if force_new_path else self.settings_path
        if not path:
            default_name = f"{Path(self.guv_path).stem}.guvcfd" if self.guv_path else "project.guvcfd"
            start_dir = self._last_project_dir()
            start = str(Path(start_dir) / default_name) if start_dir else default_name
            path, _ = QFileDialog.getSaveFileName(
                self, "Save GUV-CFD Project", start, "GUV-CFD project files (*.guvcfd)")
            if not path:
                return
        self.save_project(path)

    def save_project(self, path):
        settings = self.gather_settings()
        settings["guv_path"] = self.guv_path
        # Backfills any OpenFOAM/meshing/solver setting missing from this
        # project - see app_settings.capture_openfoam_settings's docstring
        # and app.py's _save_project (the Dash equivalent of this method).
        capture_openfoam_settings(settings, load_advanced_settings())
        with open(path, "w") as f:
            json.dump(settings, f, indent=2)
        self.settings_path = path
        self.project_label.setText(self._project_label_text())
        self._remember_project_path(path)

    def refresh_preview(self):
        try:
            self.preview.update_scene(self.room, self.gather_settings())
        except Exception as e:
            # Best-effort: a mid-edit invalid value (e.g. a field cleared
            # while typing) must never crash the tab - but silently
            # swallowing EVERY exception here previously hid a real bug
            # (room.lamps.valid() returning lamp IDs, not lamp objects)
            # for an entire round of manual testing, since a partially-
            # rendered scene looks superficially fine. Print instead of
            # fully discarding, so a genuine bug is visible in the console.
            print(f"[Preview3D] update_scene failed: {e}", file=sys.stderr)
