"""Analysis tab: view a finished run's results, export a Word report, open
the case in ParaView - the native equivalent of guvcfd.app's Analysis tab.
"""
import json
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from PySide6.QtCore import Qt

from ..case_io import read_cell_centers
from ..decay_analysis import migrate_result_keys
from ..paraview_launch import launch_paraview
from ..report import generate_report_docx
from . import helpers
from .charts import ResultsChart

_METRIC_ROWS = [
    ("ventilation_ach", "Ventilation ACH (nominal)"),
    ("ventilation_ach_measured", "Ventilation ACH (measured)"),
    ("eACH_uv_well_mixed", "eACH_uv, well-mixed (idealized ceiling)"),
    ("eACH_uv_assuming_well_mixed", "eACH_uv, CFD-fit (nominal baseline)"),
    ("eACH_uv_actual", "eACH_uv, CFD-fit (measured baseline)"),
    ("mixing_efficiency", "Mixing efficiency"),
    ("mixing_efficiency_actual", "Mixing efficiency (measured baseline)"),
    ("mechanical_mixing_efficiency_pct", "Mechanical mixing efficiency (%)"),
    ("spatial_cov_final", "Spatial coefficient of variation"),
    ("total_ach_actual", "Total ACH, effective"),
    ("reduction_pct", "Steady-state reduction (%)"),
    ("eACH_uv_steady_state", "eACH_uv, steady-state (nominal)"),
    ("eACH_uv_steady_state_corrected", "eACH_uv, steady-state (measured)"),
]

# Per-field display rules, mirroring guvcfd.app's _steady_state_summary/
# _decay_summary formatting for these same JSON keys. Some fields are 0-1
# fractions that need x100 + "%" (mixing_efficiency*, spatial_cov_final -
# confirmed 2026-08-10 this table was printing them as raw decimals, e.g.
# "0.834" instead of "83.4%", easy to misread as a much smaller number);
# some are already 0-100 percentages needing just "%" appended
# (mechanical_mixing_efficiency_pct, reduction_pct); the rest are rates
# that get a "/hr" unit suffix, matching Dash's own row text.
_PERCENT_FRACTION_FIELDS = {"mixing_efficiency", "mixing_efficiency_actual", "spatial_cov_final"}
_ALREADY_PERCENT_FIELDS = {"mechanical_mixing_efficiency_pct", "reduction_pct"}
_RATE_FIELDS = {
    "ventilation_ach", "ventilation_ach_measured", "eACH_uv_well_mixed",
    "eACH_uv_assuming_well_mixed", "eACH_uv_actual", "total_ach_actual",
    "eACH_uv_steady_state", "eACH_uv_steady_state_corrected",
}


def _format_metric_value(key, value):
    if not isinstance(value, float):
        return str(value)
    if key in _PERCENT_FRACTION_FIELDS:
        return f"{value * 100:.1f}%"
    if key in _ALREADY_PERCENT_FIELDS:
        return f"{value:.1f}%"
    if key in _RATE_FIELDS:
        return f"{value:.4g} /hr"
    return f"{value:.4g}"


class AnalysisTab(QWidget):
    def __init__(self, project_setup_tab=None, parent=None):
        super().__init__(parent)
        self.project_setup_tab = project_setup_tab
        self.case_dir = None
        self.results = None

        layout = QVBoxLayout(self)
        btn_row = QHBoxLayout()
        load_btn = QPushButton("Load results.json...")
        load_btn.clicked.connect(self.load_dialog)
        btn_row.addWidget(load_btn)
        self.export_btn = QPushButton("Export Word Report...")
        self.export_btn.clicked.connect(self.export_report)
        self.export_btn.setEnabled(False)
        btn_row.addWidget(self.export_btn)
        self.paraview_btn = QPushButton("Open in ParaView")
        self.paraview_btn.clicked.connect(self.open_paraview)
        self.paraview_btn.setEnabled(False)
        btn_row.addWidget(self.paraview_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.status_label = QLabel("No results loaded.")
        layout.addWidget(self.status_label)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter, 1)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Metric", "Value"])
        self.table.horizontalHeader().setStretchLastSection(True)
        splitter.addWidget(self.table)
        self.chart = ResultsChart()
        splitter.addWidget(self.chart)
        splitter.setSizes([420, 500])

    def load_dialog(self):
        # Defaults to (1) wherever a result was last loaded THIS session,
        # else (2) the current PROJECT's own configured OpenFOAM case
        # directory (Project Setup tab's "case-dir" field) - results.json
        # for whatever's currently open is almost always right there or a
        # combo-subfolder underneath it - else (3) the generic WSL
        # $FOAM_RUN directory every case directory lives somewhere under,
        # for when no project is open yet at all.
        project_case_dir = self.project_setup_tab.case_dir_edit.text() if self.project_setup_tab else ""
        start_dir = self.case_dir or project_case_dir or helpers.compute_default_run_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, "Open results.json", start_dir, "results.json (results.json);;All files (*)")
        if path:
            self.load_case_dir(str(Path(path).parent))

    def load_case_dir(self, case_dir):
        results_path = Path(case_dir) / "results.json"
        if not results_path.exists():
            QMessageBox.warning(self, "No results", f"{results_path} not found.")
            return
        with open(results_path) as f:
            # Older results.json files use the pre-2026-09-02 key names.
            results = migrate_result_keys(json.load(f))
        self.case_dir = case_dir
        self.results = results
        self.export_btn.setEnabled(True)
        self.paraview_btn.setEnabled(True)
        self.status_label.setText(f"Loaded {results_path}")
        self._populate_table(results)
        if "phase1" in results or "phase2" in results:
            self.chart.plot_steady_state(results)
        else:
            self.chart.plot_decay(results)

    def _populate_table(self, results):
        rows = [(key, label, results[key]) for key, label in _METRIC_ROWS
                if key in results and results[key] is not None]
        self.table.setRowCount(len(rows))
        for i, (key, label, value) in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(label))
            self.table.setItem(i, 1, QTableWidgetItem(_format_metric_value(key, value)))
        self.table.resizeColumnsToContents()

    def export_report(self):
        if not self.case_dir:
            return
        default_name = f"{Path(self.case_dir).name}_report.docx"
        path, _ = QFileDialog.getSaveFileName(self, "Save Word Report", default_name, "Word document (*.docx)")
        if not path:
            return
        try:
            generate_report_docx(self.case_dir, path)
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))
            return
        QMessageBox.information(self, "Report saved", f"Saved to {path}")

    def open_paraview(self):
        if not self.case_dir:
            return
        settings_path = Path(self.case_dir) / "run_settings.json"
        if not settings_path.exists():
            QMessageBox.warning(self, "Missing run_settings.json",
                                 f"{settings_path} not found - rerun a full simulation here first.")
            return
        with open(settings_path) as f:
            settings = json.load(f)
        try:
            points = read_cell_centers(self.case_dir, "0")
            mesh_bounds = (points[:, 0].min(), points[:, 0].max(),
                           points[:, 1].min(), points[:, 1].max(),
                           points[:, 2].min(), points[:, 2].max())
            source_center = settings.get("source_center")
            if source_center and any(v is None for v in source_center):
                source_center = None
            launch_paraview(self.case_dir, mesh_bounds, source_center=source_center)
        except Exception as e:
            QMessageBox.critical(self, "Failed to open ParaView", str(e))
