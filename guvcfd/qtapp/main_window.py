from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QDialog, QMainWindow, QMessageBox, QTabWidget, QTextBrowser, QVBoxLayout

from .. import help_content
from ..version import APP_VERSION
from .analysis_tab import AnalysisTab
from .project_setup_tab import ProjectSetupTab
from .run_tab import RunTab
from .settings_dialog import SettingsDialog

_HELP_TOPICS = [
    ("About", help_content.ABOUT),
    ("License", help_content.LICENSE_SUMMARY),
    ("References", help_content.REFERENCES),
    ("OpenFOAM Notes", help_content.OPENFOAM_NOTES),
]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"GUV-CFD v{APP_VERSION}")
        self.resize(1400, 900)

        self.project_setup_tab = ProjectSetupTab()
        self.run_tab = RunTab(self.project_setup_tab)
        self.analysis_tab = AnalysisTab(self.project_setup_tab)

        tabs = QTabWidget()
        tabs.addTab(self.project_setup_tab, "Project Setup")
        tabs.addTab(self.run_tab, "Run Simulations")
        tabs.addTab(self.analysis_tab, "Analysis of Results")
        self.setCentralWidget(tabs)
        self.tabs = tabs

        self.run_tab.run_finished.connect(self._on_run_finished)

        self._build_menu()
        self.statusBar().showMessage("Ready.")

    def _build_menu(self):
        menu = self.menuBar()
        file_menu = menu.addMenu("&File")

        new_action = QAction("New Project from .guv file...", self)
        new_action.triggered.connect(self.project_setup_tab.load_project_dialog)
        file_menu.addAction(new_action)

        # "Open Project" opens a saved .guvcfd project - the JSON file that
        # remembers this app's own inlet/outlet/fan/etc. settings, not the
        # raw .guv room design (that's "New Project" above, room defaults
        # only - see project_setup_tab.load_project vs. load_guvcfd_project).
        open_action = QAction("Open Project...", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self.project_setup_tab.open_guvcfd_dialog)
        file_menu.addAction(open_action)

        self.recent_menu = file_menu.addMenu("Open Recent")
        self.recent_menu.aboutToShow.connect(self._populate_recent_menu)

        file_menu.addSeparator()
        save_action = QAction("Save Project", self)
        save_action.setShortcut(QKeySequence.Save)
        save_action.triggered.connect(lambda: self.project_setup_tab.save_project_dialog(force_new_path=False))
        file_menu.addAction(save_action)
        save_as_action = QAction("Save Project As...", self)
        save_as_action.setShortcut(QKeySequence.SaveAs)
        save_as_action.triggered.connect(lambda: self.project_setup_tab.save_project_dialog(force_new_path=True))
        file_menu.addAction(save_as_action)

        settings_action = QAction("Settings...", self)
        settings_action.triggered.connect(self._open_settings)
        menu.addAction(settings_action)

        help_menu = menu.addMenu("&Help")
        for label, content in _HELP_TOPICS:
            action = QAction(label, self)
            action.triggered.connect(lambda checked=False, l=label, c=content: self._show_help(l, c))
            help_menu.addAction(action)

    def _populate_recent_menu(self):
        self.recent_menu.clear()
        recents = self.project_setup_tab.recent_projects()
        if not recents:
            empty = QAction("(no recent projects)", self)
            empty.setEnabled(False)
            self.recent_menu.addAction(empty)
            return
        for path in recents:
            action = QAction(path, self)
            action.triggered.connect(lambda checked=False, p=path: self.project_setup_tab.open_recent_project(p))
            self.recent_menu.addAction(action)

    def _open_settings(self):
        dlg = SettingsDialog(self)
        dlg.exec()

    def _show_help(self, title, markdown_text):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(700, 600)
        layout = QVBoxLayout(dlg)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setMarkdown(markdown_text)
        layout.addWidget(browser)
        dlg.exec()

    def _on_run_finished(self):
        # A sweep (more than 1 Z x ACH combination) has no single
        # results.json to auto-load - each combination gets its own
        # subfolder, browsable from Analysis of Results via "Load
        # results.json..." - only a genuine 1-combination run auto-loads.
        if self.run_tab._active != "single":
            state = self.run_tab.sweep_state
            if state.status == "error":
                QMessageBox.critical(self, "Sweep failed", state.error or "Unknown error - see the log.")
            self.statusBar().showMessage(f"Sweep {state.status}.")
            return

        state = self.run_tab.state
        if state.status == "done" and state.case_dir:
            self.analysis_tab.load_case_dir(state.case_dir)
            self.tabs.setCurrentWidget(self.analysis_tab)
            self.statusBar().showMessage(f"Run finished - results loaded from {state.case_dir}")
        elif state.status == "error":
            QMessageBox.critical(self, "Run failed", state.error or "Unknown error - see the log.")
        else:
            self.statusBar().showMessage(f"Run {state.status}.")
