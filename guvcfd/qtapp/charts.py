"""Result curve charts via embedded matplotlib - the native replacement for
guvcfd.result_figures's Plotly figures (same underlying results.json data,
just drawn with a toolkit that embeds natively in Qt instead of a browser).
"""
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure


class ResultsChart(FigureCanvasQTAgg):
    def __init__(self, parent=None):
        self.fig = Figure(figsize=(5, 3.5), tight_layout=True)
        super().__init__(self.fig)
        self.setParent(parent)
        self.ax = self.fig.add_subplot(111)

    def clear(self):
        self.ax.clear()
        self.draw()

    def plot_decay(self, results):
        self.ax.clear()
        curve = results.get("decay_curve") or {}
        t, T = curve.get("t_seconds"), curve.get("volAverage_T")
        if t and T:
            self.ax.plot(t, T, ".", markersize=3, color="tab:blue", label="volAverage(T)")
            self.ax.set_yscale("log")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Room-average concentration T")
        self.ax.set_title("Decay curve")
        self.ax.legend(loc="best")
        self.draw()

    def plot_steady_state(self, results):
        self.ax.clear()
        phase1 = (results.get("phase1") or {}).get("live") or {}
        phase2 = (results.get("phase2") or {}).get("live") or {}
        plotted = False
        for phase, color, label in ((phase1, "tab:orange", "Phase 1 (no UV)"),
                                     (phase2, "tab:green", "Phase 2 (+UV)")):
            t, T = phase.get("t"), phase.get("T")
            if t and T:
                self.ax.plot(t, T, ".", markersize=3, color=color, label=label)
                plotted = True
        self.ax.set_xlabel("Iteration")
        self.ax.set_ylabel("Room-average concentration T")
        self.ax.set_title("Steady-state buildup")
        if plotted:
            self.ax.legend(loc="best")
        self.draw()
