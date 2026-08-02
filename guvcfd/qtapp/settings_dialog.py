"""Advanced settings dialog - the native equivalent of guvcfd.app's
Settings modal. Builds its form from app_settings.ADVANCED_SETTINGS_DEFAULTS
(so it never drifts out of sync with what keys actually exist), with a
friendly label + hover tooltip per field from _FIELD_INFO below rather than
showing raw internal dict keys."""
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)

from ..app_settings import ADVANCED_SETTINGS_DEFAULTS, load_advanced_settings, save_advanced_settings

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
    "mesh-cell-size": ("Mesh cell size (m)",
                        "Target grid spacing for the room mesh. Smaller = finer mesh, more accurate, "
                        "much slower."),
    "uv-zone-bins": ("UV inactivation-rate bins",
                      "How many discrete UV-strength zones the continuous fluence field is grouped "
                      "into for the solver."),
    "momentum-relaxation": ("Momentum under-relaxation",
                             "SIMPLE under-relaxation factor for velocity/turbulence during flow "
                             "convergence - lower is more stable but slower."),
    "scalar-relaxation": ("Contaminant (T) under-relaxation",
                           "Under-relaxation factor for the contaminant field."),
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
    def __init__(self, parent=None):
        super().__init__(parent)
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

        current = load_advanced_settings()
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
        save_advanced_settings(values)
        self.accept()
