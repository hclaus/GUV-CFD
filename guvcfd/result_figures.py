"""Plotly result-curve figures shared between app.py's Analysis tab and
report.py's .docx export - pure functions (just plotly + math, no Dash
dependency) so report.py can reuse them without a circular import back
into app.py.
"""
import math
import plotly.graph_objs as go


def _window_rect_and_line(fig, t, T_ss1, T_ss, window_span, color, shift=0.0):
    """Shade the trailing moving-average window (see decay_analysis.
    windowed_stats) on a phase's curve and mark its mean - same visual
    design validated in the live-volAverage comparison artifact. `shift`
    offsets x into steady_state_figure's shared timeline (phase 2 only).
    """
    if not t or window_span is None:
        return
    x0, x1 = shift + t[-1] - window_span, shift + t[-1]
    pct_mean = 100 * T_ss / T_ss1
    fig.add_vrect(x0=x0, x1=x1, fillcolor=color, opacity=0.12, line_width=0)
    fig.add_shape(type="line", x0=x0, x1=x1, y0=pct_mean, y1=pct_mean,
                  line=dict(color=color, width=1.5, dash="dot"))


def steady_state_figure(result):
    """T over time as a percentage of phase 1's steady state (100%), phase
    1 and phase 2 plotted on one continuous linear timeline (phase 2
    shifted to start where phase 1 ends) so the UV-on transition and its
    reduction read directly off the curve. Time axis is linear - the
    underlying OpenFOAM write schedule is what's log-spaced, not this plot.

    Uses the dense live per-iteration series (result["phase1/2"]["live"])
    when present - much less noisy-looking than the sparse write_interval
    samples it replaces - with the trailing moving-average window (T_ss is
    now that window's mean, not a single last sample - see
    decay_analysis.windowed_stats) shaded and its mean marked. Falls back
    to the old sparse decay_curve for results.json predating live tracking.
    """
    p1, p2 = result["phase1"], result["phase2"]
    T_ss1 = p1["T_ss"] or 1.0
    curve1 = p1.get("live", p1["decay_curve"])
    t1 = curve1["t"]
    T1 = curve1["T"]
    t1_end = t1[-1] if t1 else 0.0

    curve2 = p2.get("live", p2["decay_curve"])
    t2 = curve2["t"]
    T2 = curve2["T"]
    t2_shifted = [t1_end + v for v in t2]

    pct1 = [100 * v / T_ss1 for v in T1]
    pct2 = [100 * v / T_ss1 for v in T2]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=t1, y=pct1, mode="lines", name="Phase 1 (no UV)",
                              line=dict(color="#e67e22", width=1.5)))
    fig.add_trace(go.Scatter(x=t2_shifted, y=pct2, mode="lines", name="Phase 2 (UV on)",
                              line=dict(color="#2ecc71", width=1.5)))
    _window_rect_and_line(fig, t1, T_ss1, p1["T_ss"], p1.get("T_ss_window_span"), "#e67e22")
    _window_rect_and_line(fig, t2, T_ss1, p2["T_ss"], p2.get("T_ss_window_span"), "#2ecc71", shift=t1_end)
    fig.add_hline(y=100, line_dash="dot", line_color="gray",
                  annotation_text="Phase 1 steady state (100%)", annotation_position="top left")
    pct2_ss = 100 * p2["T_ss"] / T_ss1
    fig.add_hline(y=pct2_ss, line_dash="dot", line_color="#2ecc71",
                  annotation_text=f"Phase 2 steady state ({pct2_ss:.1f}%)", annotation_position="bottom left")
    fig.add_vline(x=t1_end, line_dash="dash", line_color="gray", annotation_text="UV on")
    fig.update_layout(
        xaxis_title="Time (s)", yaxis_title="T (% of phase 1 steady state)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=50, r=20, t=30, b=45),
    )
    return fig


def decay_figure(result):
    """Actual CFD decay curve plus two idealized well-mixed reference curves
    (pure ventilation, and ventilation+UV at the well-mixed eACH estimate)
    computed from the same T[0] starting value - so the gap between the real
    (CFD) curve and each reference visually shows how much imperfect mixing
    slows disinfection versus the idealized box-model assumption. Log y-axis
    since decay is exponential - a straight line here is a pure exponential,
    and curvature/kinks reveal where the real mixing deviates from one.
    """
    curve = result["decay_curve"]
    t, T = curve["t_seconds"], curve["volAverage_T"]
    T0 = T[0] if T else 1.0

    lambda_vent = result["ventilation_ach"] / 3600.0
    lambda_well_mixed = lambda_vent + result["eACH_uv_well_mixed"] / 3600.0
    ach_curve = [T0 * math.exp(-lambda_vent * ti) for ti in t]
    well_mixed_curve = [T0 * math.exp(-lambda_well_mixed * ti) for ti in t]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=t, y=T, mode="lines+markers", name="volAverage(T) - actual (CFD)",
                              line=dict(color="#3498db", width=2)))
    fig.add_trace(go.Scatter(x=t, y=ach_curve, mode="lines",
                              name=f"Ventilation ACH only ({result['ventilation_ach']:.3g}/hr)",
                              line=dict(color="#95a5a6", width=2, dash="dash")))
    fig.add_trace(go.Scatter(x=t, y=well_mixed_curve, mode="lines",
                              name=f"Well-mixed, ACH+eACH_uv "
                                   f"({result['ventilation_ach'] + result['eACH_uv_well_mixed']:.3g}/hr)",
                              line=dict(color="#e67e22", width=2, dash="dash")))
    fig.update_layout(
        xaxis_title="Time (s)", yaxis_title="volAverage(T)", yaxis_type="log",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=50, r=20, t=30, b=45),
    )
    return fig
