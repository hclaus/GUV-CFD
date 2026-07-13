"""Generate a one-page-ish .docx summary of a completed decay run: room
setup parameters, a rendered picture of the case setup (inlet/outlet/fan/
lamps), and the key result numbers - for sharing outside the GUI itself.
"""
import json
from pathlib import Path

from docx import Document
from docx.shared import Inches
from guv_calcs import Project

from .visualization import plot_case

_ROW_LABELS_ROOM = [
    ("Room dimensions", lambda r, s: f"{r.x:.3g} x {r.y:.3g} x {r.z:.3g} {r.units}"),
    ("Lamps", lambda r, s: str(len(r.lamps))),
    ("Ventilation ACH", lambda r, s: f"{s['ach']:.3g} /hr"),
    ("UV inactivation constant Z", lambda r, s: f"{s['z-value']:.3g} m^2/J"),
    ("Inlet", lambda r, s: f"{s['inlet-wall']}, y={s['inlet-y-input']:.3g}m "
                           f"z={s['inlet-z-input']:.3g}m, size={s['inlet-size-w']:.3g}x"
                           f"{s['inlet-size-h']:.3g}m"),
    ("Outlet", lambda r, s: f"{s['outlet-wall']}, y={s['outlet-y-input']:.3g}m "
                            f"z={s['outlet-z-input']:.3g}m, size={s['outlet-size-w']:.3g}x"
                            f"{s['outlet-size-h']:.3g}m"),
]

_ROW_LABELS_FAN = [
    ("Mixing fan", lambda s: f"{s['fan-speed']:.3g} m/s, direction={s['fan-direction']}, "
                             f"position=({s['fan-x-input']:.3g}, {s['fan-y-input']:.3g}, "
                             f"{s['fan-z-input']:.3g})m, radius={s['fan-radius']:.3g}m"),
]

_ROW_LABELS_RESULTS_DECAY = [
    ("Average fluence rate", lambda res: f"{res['fluence_mean']:.4g} µW/cm²"
                                          if res.get("fluence_mean") is not None else "n/a"),
    ("Ventilation ACH (nominal)", lambda res: f"{res['ventilation_ach']:.3g} /hr"),
    ("eACH_uv, well-mixed (idealized)", lambda res: f"{res['eACH_uv_well_mixed']:.4g} /hr"),
    ("eACH_uv, effective (CFD-fit)", lambda res: f"{res['eACH_uv_effective']:.4g} /hr"),
    ("Mixing efficiency", lambda res: f"{res['mixing_efficiency'] * 100:.1f}%"
                                        if res.get("mixing_efficiency") is not None else "n/a"),
    ("Total ACH, effective", lambda res: f"{res.get('total_ach_effective', 0):.3g} /hr"),
    ("Simulated duration", lambda res: f"{res['decay_curve']['t_seconds'][-1]:.4g} s"
                                         if res.get("decay_curve", {}).get("t_seconds") else "n/a"),
]

# Corrected (UV-off control) numbers - only present when that option was used.
_ROW_LABELS_RESULTS_DECAY_CORRECTED = [
    ("Ventilation ACH (measured, UV-off control)",
     lambda res: f"{res['ventilation_ach_measured']:.4g} /hr"),
    ("eACH_uv, effective (corrected)", lambda res: f"{res['eACH_uv_effective_corrected']:.4g} /hr"),
    ("Mixing efficiency (corrected)", lambda res: f"{res['mixing_efficiency_corrected'] * 100:.1f}%"
                                                     if res.get("mixing_efficiency_corrected") is not None
                                                     else "n/a"),
]

_ROW_LABELS_RESULTS_STEADY_STATE = [
    ("Average fluence rate", lambda res: f"{res['fluence_mean']:.4g} µW/cm²"
                                          if res.get("fluence_mean") is not None else "n/a"),
    ("Target well-mixed steady-state T", lambda res: f"{res.get('target_T_ss', '?')}"),
    ("Phase 1 T_ss (no UV)", lambda res: f"{res['phase1']['T_ss']:.4g} "
                                          f"({'plateaued' if res['phase1']['converged'] else 'NOT fully plateaued'}, "
                                          f"{res['phase1']['iterations']} iterations)"),
    ("Phase 2 T_ss (UV on)", lambda res: f"{res['phase2']['T_ss']:.4g} "
                                          f"({'plateaued' if res['phase2']['converged'] else 'NOT fully plateaued'}, "
                                          f"{res['phase2']['iterations']} iterations)"),
    ("Reduction", lambda res: f"{res['reduction_pct']:.1f}%"),
    ("eACH_uv (steady-state method)", lambda res: f"{res['eACH_uv_steady_state']:.4g} /hr"),
]


def _add_kv_table(doc, rows):
    table = doc.add_table(rows=0, cols=2)
    table.style = "Light Grid Accent 1"
    for label, value in rows:
        cells = table.add_row().cells
        cells[0].text = label
        cells[1].text = value
    return table


def generate_report_docx(case_dir, out_path):
    """Build the report from run_settings.json + results.json in case_dir.
    Raises FileNotFoundError with a clear message if either is missing (no
    completed run to report on yet).
    """
    settings_path = Path(case_dir) / "run_settings.json"
    results_path = Path(case_dir) / "results.json"
    if not settings_path.exists() or not results_path.exists():
        raise FileNotFoundError(
            f"{case_dir} doesn't have both run_settings.json and results.json - "
            f"run a full simulation to completion first."
        )
    with open(settings_path) as f:
        settings = json.load(f)
    with open(results_path) as f:
        results = json.load(f)

    guv_path = settings.get("guv_path")
    if not guv_path:
        raise FileNotFoundError(
            f"{case_dir}/run_settings.json has no recorded project path - "
            f"this case directory predates report support; rerun a full "
            f"simulation here to enable report generation."
        )
    project = Project.load(guv_path)
    room = next(iter(project.rooms.values()))

    fan_kwargs = {}
    if settings.get("fan-enable"):
        direction = (0, 0, -1) if settings["fan-direction"] == "down" else (0, 0, 1)
        fan_kwargs = dict(
            fan_speed=settings["fan-speed"], fan_disk_radius=settings["fan-radius"],
            fan_disk_thickness=settings["fan-thickness"],
            fan_center=(settings["fan-x-input"], settings["fan-y-input"], settings["fan-z-input"]),
            fan_direction=direction,
        )
    fig = plot_case(
        room,
        inlet_wall=settings["inlet-wall"],
        inlet_center=(settings["inlet-y-input"] / room.y, settings["inlet-z-input"] / room.z),
        inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
        outlet_wall=settings["outlet-wall"],
        outlet_center=(settings["outlet-y-input"] / room.y, settings["outlet-z-input"] / room.z),
        outlet_size=(settings["outlet-size-w"], settings["outlet-size-h"]),
        title="", **fan_kwargs,
    )
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), width=900, height=650)
    image_path = Path(out_path).with_suffix(".preview.png")
    fig.write_image(str(image_path))

    doc = Document()
    doc.add_heading("GUV-CFD Simulation Report", level=1)
    doc.add_paragraph(f"Case directory: {case_dir}")

    doc.add_heading("Room Setup", level=2)
    _add_kv_table(doc, [(label, fn(room, settings)) for label, fn in _ROW_LABELS_ROOM])
    if settings.get("fan-enable"):
        _add_kv_table(doc, [(label, fn(settings)) for label, fn in _ROW_LABELS_FAN])

    doc.add_heading("Case Setup", level=2)
    doc.add_picture(str(image_path), width=Inches(6.0))

    doc.add_heading("Results", level=2)
    if "phase1" in results:
        _add_kv_table(doc, [(label, fn(results)) for label, fn in _ROW_LABELS_RESULTS_STEADY_STATE])
    else:
        _add_kv_table(doc, [(label, fn(results)) for label, fn in _ROW_LABELS_RESULTS_DECAY])
        if results.get("ventilation_ach_measured") is not None:
            _add_kv_table(doc, [(label, fn(results)) for label, fn in _ROW_LABELS_RESULTS_DECAY_CORRECTED])

    doc.save(out_path)
    image_path.unlink(missing_ok=True)
    return out_path
