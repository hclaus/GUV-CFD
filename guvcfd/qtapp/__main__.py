"""Entry point: `python -m guvcfd.qtapp`."""
import sys

from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle

from .main_window import MainWindow


class _FastTooltipStyle(QProxyStyle):
    """Qt's default tooltip wake-up delay (SH_ToolTip_WakeUpDelay) is 700ms
    on every style tested - halved here per explicit user request, since a
    tooltip is the app's only way to surface the original web app's
    explanatory "help text" now (see project_setup_tab._add_row)."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.SH_ToolTip_WakeUpDelay:
            base = super().styleHint(hint, option, widget, returnData)
            return max(1, base // 2)
        return super().styleHint(hint, option, widget, returnData)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("GUV-CFD")
    app.setStyle(_FastTooltipStyle(app.style()))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
