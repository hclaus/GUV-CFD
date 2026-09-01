"""Advanced settings dialog - the native equivalent of guvcfd.app's
Settings modal. Builds its form from app_settings.ADVANCED_SETTINGS_DEFAULTS
(so it never drifts out of sync with what keys actually exist), with a
friendly label + hover tooltip per field from _FIELD_INFO below rather than
showing raw internal dict keys."""
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel, QMessageBox, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

from ..app_settings import (
    ADVANCED_SETTINGS_DEFAULTS, load_advanced_settings, merge_project_openfoam_settings,
    PROJECT_OPENFOAM_SETTINGS_KEYS, save_advanced_settings,
)

# key -> (friendly label, hover tooltip). Anything missing here falls back
# to showing the raw key, with no tooltip - never a hard error.
_FIELD_INFO = {
    "flow-rel-tol": ("Flow convergence tolerance (%)",
                      "How much the room-average pressure is allowed to change between chunks before "
                      "the airflow is considered settled. Smaller = stricter/slower."),
    "flow-max-iterations": ("Flow convergence iteration cap",
                             "Hard ceiling on total simpleFoam iterations spent settling the airflow "
                             "before giving up (or accepting a stable oscillation)."),
    "plateau-rel-tol": ("Steady-state plateau tolerance (%)",
                         "Coefficient-of-variation threshold used to decide a steady-state phase has "
                         "genuinely settled."),
    "pimple-delta-t": ("Decay solver time step (s)",
                        "The transient decay solver's own time step size."),
    "max-co": ("Decay solver Courant cap (maxCo)",
               "Upper bound on the Courant number pimpleFoam's adaptive time step is allowed to "
               "reach - higher lets the solver take bigger steps (faster) but pushes closer to the "
               "limit nOuterCorrectors=3 (fixed in the template) can still keep stable/accurate "
               "each step. 5 is the original conservative default; a live production sweep "
               "confirmed 10 stays numerically stable - see ANALYSIS_LOG.md before pushing higher."),
    "max-concurrent-solves": ("Max concurrent solves (sweeps)",
                               "How many OpenFOAM processes a sweep runs at once. Was 9 (CPU-core "
                               "headroom only); lowered to 5 after a real overnight sweep crashed itself "
                               "in 5 waves from resource contention. Raise only if you've confirmed your "
                               "machine has the RAM for it - lower this if a sweep dies overnight."),
    "mesh-cell-size": ("Mesh cell size (m)",
                        "Target grid spacing for the room mesh. Smaller = finer mesh, more accurate, "
                        "much slower."),
    "uv-zone-bins": ("UV inactivation-rate bins",
                      "How many discrete UV-strength zones the continuous fluence field is grouped "
                      "into for the solver."),
    "momentum-relaxation": ("Momentum under-relaxation",
                             "SIMPLE under-relaxation factor for velocity/turbulence during flow "
                             "convergence - lower is more stable but slower. Lower this number a bit "
                             "if oscillation is a problem (Phase 1/2 T never settling)."),
    "scalar-relaxation": ("Contaminant (T) under-relaxation",
                           "Under-relaxation factor for the contaminant field. Ignored when "
                           "'Use adaptive T relaxation' below is on."),
    "adaptive-t-relaxation": ("Use adaptive T relaxation",
                               "It has been found that for high Z*fluencerate values, significantly "
                               "lower T-relaxation values are needed to prevent crashing. An adaptive "
                               "equation has been established to adjust T relaxation automatically for "
                               "non crashing and shortest simulation times."),
    "t-clamp-decay-enabled": ("Use T divergence clamp",
                               "Independent of adaptive T relaxation above (that tunes how the solver "
                               "approaches a diverging cell; this catches the divergence itself if it "
                               "happens anyway). When on, any cell whose T strays outside [0, Tmax] "
                               "each iteration is replaced by a locally sink-decayed value instead of "
                               "a hard reset, keeping the correction physically motivated. Tmax is set "
                               "per-run as the multiplier below times Phase 1's own converged "
                               "source-zone max T."),
    "t-clamp-decay-multiplier": ("T divergence clamp multiplier",
                                  "Tmax = this value times Phase 1's own converged source-zone max T. "
                                  "Only used when the T divergence clamp above is on."),
    "phase1-tmax-multiplier": ("Phase 1 clamp ceiling (x target T_ss)",
                                "Phase 1's own Tmax, as a multiple of the design target room "
                                "average. The ceiling is a divergence backstop, not a peak shaver "
                                "- the T<0 floor does the real work and is always on. A "
                                "source-zone peak around 6x the room average is normal, so 10-20x "
                                "leaves the ceiling clear of real physics while still catching a "
                                "cell running away toward 1e80."),
    "decay-extend-to-target": ("Extend decay runs until the target is met",
                                "Decay mode only. The run lengths above are computed from an ASSUMED "
                                "removal rate (nominal ACH for the control, ACH + well-mixed eACH for "
                                "the UV-on run). When on, each run is re-checked against the rate it "
                                "ACTUALLY achieved and continued if it fell short - without this a "
                                "poorly-mixed room silently gets a run far too short to measure, and "
                                "reports a negative eACH rather than an error."),
    "decay-max-total-time": ("Decay run hard cap (s)",
                              "Maximum simulated seconds for ONE decay run, extensions included. If a "
                              "run still hasn't met its target here it is reported as capped and its "
                              "eACH/mixing figures flagged unreliable, rather than quietly accepted."),
    "scalar-transport-ncorr": ("Contaminant solver outer correctors",
                                "How many times the contaminant equation is re-solved per timestep - "
                                "needs to be >0 for the relaxation factor above to matter at all."),
    "scalar-transport-tolerance": ("Contaminant solver residual target",
                                    "Residual threshold the contaminant solver's outer-corrector loop "
                                    "aims for each timestep."),
    "t-infinity-early-stop-enabled": ("Enable early-stop via T-infinity extrapolation",
                                       "Lets a steady-state phase finish early once its curve-fit "
                                       "extrapolation to n->infinity looks stable."),
    "t-infinity-rel-tol": ("T-infinity stability tolerance (%)",
                            "How close consecutive extrapolation fits must be to call it stable."),
    "phase1-require-stable-extrapolation": ("Require stable extrapolation before accepting Phase 1",
                                             "Stricter (but can pause a run needing a decision) - off by "
                                             "default. Not supported in the sweep/batch path."),
    "phase-chunk-size": ("Phase chunk size (iterations)",
                          "How many iterations a steady-state Phase 1/2 chunk runs before checking in."),
    "phase-write-interval": ("Phase write interval (iterations)",
                              "How often Phase 1/2 writes a time directory to disk."),
    "keep-all-timesteps": ("Keep every timestep on disk (for ParaView)",
                            "Opt-in - keeps every written time directory instead of only the latest, "
                            "for animating in ParaView. Uses much more disk space."),
    "deltat-scaling-enabled": ("Enable residence-time-scaled deltaT (steady-state)",
                                "Speeds up low-ACH steady-state runs for free by scaling the pseudo-"
                                "time step instead of running more iterations. Not used by this app's "
                                "simplified steady-state path yet - see README."),
    "deltat-effective-fraction": ("DeltaT scaling: effective-ACH derating fraction",
                                   "Conservative derating since measured ACH/eACH typically runs below "
                                   "nominal."),
    "deltat-target-fraction": ("DeltaT scaling: target residence-time fraction",
                                "How close to full steady-state the deltaT scaling targets."),
    "flow-converged-chunks": ("Consecutive chunks to call it converged",
                              "How many convergence-check chunks in a ROW must each stay within the "
                              "flow convergence tolerance before the flow field is accepted. Do not "
                              "set this to 1: a single small chunk-to-chunk change is smallest "
                              "exactly at a turning point of an oscillation, so a one-chunk test "
                              "fires at the peaks and troughs of a still-swinging field and calls "
                              "it converged."),
    "oscillation-window": ("Oscillation-acceptance window (chunks)",
                            "How many recent flow-convergence chunks are compared to judge a bounded, "
                            "non-growing oscillation as 'good enough'."),
    "oscillation-growth-tol": ("Oscillation growth tolerance (ratio)",
                                "How much the oscillation's amplitude is allowed to grow between "
                                "windows and still be accepted as bounded."),
    "ach-delivery-tol": ("ACH delivery tolerance (%)",
                          "How far the CFD-measured airflow is allowed to differ from the nominal "
                          "target before being flagged."),
    "mass-balance-tol": ("Mass balance tolerance (%)",
                          "Steady-state Phase 1's informational cross-check: measured outlet removal "
                          "vs. the known injection rate."),
    "phase1-t-initial": ("Phase 1 starting concentration",
                          "Phase 1's initial contaminant field value (0 = cold start)."),
    "phase1-extrapolation-streak": ("Required stable-fit streak (Phase 1)",
                                     "Consecutive stable T-infinity fits required before accepting "
                                     "Phase 1, when that gate is enabled above."),
    "phase1-settling-safety-multiplier": ("Phase 1 settling safety multiplier",
                                           "Safety factor applied to the naive ACH-based iteration "
                                           "estimate for Phase 1's starting budget."),
    "phase1-max-iterations-ceiling": ("Phase 1 iteration hard ceiling",
                                       "Absolute cap on Phase 1 iterations regardless of the safety "
                                       "multiplier above."),
    "decay-ach-min-fraction": ("Decay UV-off control target (%)",
                                "Target reduction fraction for the UV-off control run's duration."),
    "decay-each-min-fraction": ("Decay UV-on baseline target (%)",
                                 "Baseline target reduction fraction for the UV-on run's duration."),
    "decay-each-max-fraction": ("Decay UV-on target when cheap (%)",
                                 "Higher target used instead of the baseline above when it's cheap to "
                                 "reach (high combined removal rate)."),
    "keep-shared-scratch-dirs": ("Keep shared sweep scratch directories",
                                  "Troubleshooting opt-in for batch sweeps - not used by this app's "
                                  "single-run-only path yet."),
}


class SettingsDialog(QDialog):
    def __init__(self, parent=None, project_tab=None):
        """project_tab: the currently loaded ProjectSetupTab, if any - lets
        this dialog show and edit that project's own currently-EFFECTIVE
        PROJECT_OPENFOAM_SETTINGS_KEYS values (its own pinned overrides if
        it has any, else the global default) instead of a value
        disconnected from whatever project is actually open, and lets a
        change made here actually stick to that project on its next save
        (see _save below and ProjectSetupTab.apply_project_openfoam_overrides's
        own docstring for the incident this closes - previously this
        dialog had no connection to a loaded project at all, so even a
        deliberate, in-session change here could never survive a save).
        None (e.g. no project loaded yet) falls back to editing only the
        global defaults, exactly like before.
        """
        super().__init__(parent)
        self.project_tab = project_tab
        self.setWindowTitle("Advanced Settings")
        self.resize(560, 640)
        self.fields = {}

        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        scroll.setWidget(inner)
        form = QFormLayout(inner)
        layout.addWidget(scroll, 1)

        adv = load_advanced_settings()
        current = dict(adv)
        if project_tab is not None:
            current.update(merge_project_openfoam_settings(project_tab.gather_settings(), adv))
        for key, default in ADVANCED_SETTINGS_DEFAULTS.items():
            value = current.get(key, default)
            label_text, tooltip = _FIELD_INFO.get(key, (key, None))
            if isinstance(default, bool):
                w = QCheckBox()
                w.setChecked(bool(value))
            elif isinstance(default, int):
                w = QSpinBox()
                w.setRange(-10 ** 9, 10 ** 9)
                w.setValue(int(value))
            else:
                w = QDoubleSpinBox()
                w.setRange(-1e9, 1e9)
                w.setDecimals(6)
                w.setValue(float(value))
            if tooltip:
                w.setToolTip(tooltip)
            self.fields[key] = w
            label = QLabel(label_text)
            if tooltip:
                label.setToolTip(tooltip)
            form.addRow(label, w)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self):
        values = {}
        for key, w in self.fields.items():
            if isinstance(w, QCheckBox):
                values[key] = w.isChecked()
            elif isinstance(w, QSpinBox):
                values[key] = w.value()
            else:
                values[key] = w.value()
        # Defense-in-depth, kept even after fixing the actual bug this guarded
        # against (steady_state_pipeline._rename_chunk_time_dirs used to rename
        # every numbered directory on disk, not just the current chunk's own -
        # confirmed corrupting a real case directory when both these were on
        # together). Mirrors app._save_settings's identical guard - see that
        # function's own comment. The two features are independent, and
        # now-fixed, so this block is deliberately conservative rather than
        # load-bearing - remove it once the fix has enough runs behind it to
        # trust the combination.
        if values.get("t-infinity-early-stop-enabled") and values.get("keep-all-timesteps"):
            QMessageBox.warning(
                self, "Not saved",
                'Not saved - "Enable T∞ early stopping" and "Keep all time steps for ParaView" '
                "can't both be on at once (a directory-naming bug was found in this combination; "
                "it's since been fixed, but this block is left in as a precaution for now). "
                "Turn one off before saving.")
            return
        save_advanced_settings(values)
        if self.project_tab is not None:
            # Makes this change stick to the LOADED project too, not just
            # the global default it also always updates above - see this
            # class's own __init__ docstring for the incident this closes.
            self.project_tab.apply_project_openfoam_overrides(
                {key: value for key, value in values.items() if key in PROJECT_OPENFOAM_SETTINGS_KEYS})
        self.accept()
