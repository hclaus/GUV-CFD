"""Generate a one-page-ish .docx summary of a completed decay run: room
setup parameters, a rendered picture of the case setup (inlet/outlet/fan/
lamps), and the key result numbers - for sharing outside the GUI itself.
"""
import json
import tempfile
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.shared import Inches
from guv_calcs import Project

from .contaminant_source import compute_source_strength
from .monitoring_points import mixing_uniformity_note
from .result_figures import decay_figure, steady_state_figure
from .system_info import get_system_info
from .visualization import center_frac_for_wall, plot_case


def _format_elapsed(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _run_timing(case_dir, results):
    """(started_at, elapsed_seconds) for the "Simulation date"/"Total
    elapsed time" report rows - from results.json's own run_started_at/
    run_elapsed_seconds if this run recorded them, else a rough fallback
    from run_settings.json's/results.json's file-modified times (written
    near the start and end of a run respectively) for older or
    still-in-progress case directories that predate that tracking.
    """
    if results.get("run_started_at") is not None:
        started_at = datetime.fromisoformat(results["run_started_at"])
        return started_at, results.get("run_elapsed_seconds")
    try:
        start_mtime = (Path(case_dir) / "run_settings.json").stat().st_mtime
        end_mtime = (Path(case_dir) / "results.json").stat().st_mtime
        return datetime.fromtimestamp(start_mtime), max(0, end_mtime - start_mtime)
    except OSError:
        return None, None

# What "T" actually is - shown once under the Results heading in both the
# .docx report and the Analysis tab (imported from here by app.py) since
# neither this simulation nor OpenFOAM itself assigns it a physical unit.
T_FIELD_NOTE = (
    "Note: T is the OpenFOAM field name for the transported scalar this "
    "whole simulation tracks - the substance being reduced, per unit volume. "
    "In a GUV disinfection context this is typically a pathogen "
    "concentration, e.g. CFU/m³ (colony-forming units per cubic meter) or "
    "an equivalent airborne-contaminant unit; the CFD itself is "
    "unit-agnostic and just tracks relative concentration."
)

_ROW_LABELS_ROOM = [
    ("Room dimensions", lambda r, s: f"{r.x:.3g} x {r.y:.3g} x {r.z:.3g} {r.units}"),
    ("Lamps", lambda r, s: str(len(r.lamps))),
    ("Ventilation ACH", lambda r, s: f"{s['ach']:.3g} /hr"),
    ("UV inactivation constant Z", lambda r, s: f"{s['z-value']:.3g} cm²/mJ"),
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
    ("eACH_uv, well-mixed (idealized: Z x E_avg)", lambda res: f"{res['eACH_uv_well_mixed']:.4g} /hr"),
    ("eACH_uv, CFD-fit (nominal ventilation ACH)", lambda res: f"{res['eACH_uv_effective']:.4g} /hr"),
    ("Mixing efficiency", lambda res: f"{res['mixing_efficiency'] * 100:.1f}%"
                                        if res.get("mixing_efficiency") is not None else "n/a"),
    ("Total ACH, effective", lambda res: f"{res.get('total_ach_effective', 0):.3g} /hr"),
    ("Simulated duration", lambda res: f"{res['decay_curve']['t_seconds'][-1]:.4g} s"
                                         if res.get("decay_curve", {}).get("t_seconds") else "n/a"),
]

# Ventilation ACH is *measured* here (from a UV-off control run) instead of
# assumed at its nominal design value, so the eACH_uv/mixing-efficiency
# numbers below isolate UV's own contribution more accurately - see
# decay_analysis.compute_effective_eACH's docstring. Only present when that
# control run was used.
_ROW_LABELS_RESULTS_DECAY_CORRECTED = [
    ("Ventilation ACH (measured, UV-off control)",
     lambda res: f"{res['ventilation_ach_measured']:.4g} /hr"),
    ("eACH_uv, CFD-fit (measured ventilation ACH)",
     lambda res: f"{res['eACH_uv_effective_corrected']:.4g} /hr"),
    ("Mixing efficiency (using measured ventilation ACH)",
     lambda res: f"{res['mixing_efficiency_corrected'] * 100:.1f}%"
                 if res.get("mixing_efficiency_corrected") is not None else "n/a"),
]

_ROW_LABELS_RESULTS_STEADY_STATE_BEFORE_PHASES = [
    ("Average fluence rate", lambda res: f"{res['fluence_mean']:.4g} µW/cm²"
                                          if res.get("fluence_mean") is not None else "n/a"),
    ("Target well-mixed steady-state T", lambda res: f"{res.get('target_T_ss', '?')}"),
    ("Source injection rate (total, room-wide)",
     lambda res: f"{res['injection_rate_total']:.4g} T-units/s (see T note below)"
                 if res.get("injection_rate_total") is not None else "n/a"),
]

_ROW_LABELS_RESULTS_STEADY_STATE_AFTER_PHASES = [
    ("Reduction", lambda res: f"{res['reduction_pct']:.1f}%"),
    ("Theoretical eACH_uv, steady-state (well mixed ventilation eACH = Z*Eavg)",
     lambda res: f"{res['eACH_uv_well_mixed']:.4g} /hr"
                 if res.get("eACH_uv_well_mixed") is not None else "n/a"),
]


def _phase_ss_rows(phase_num, uv_note, phase):
    """Steady-state phase1/phase2 rows: a trailing-window moving average +
    CV (see decay_analysis.windowed_stats) when the live per-iteration data
    is present, falling back to the old plain-T_ss row (exact original
    wording, e.g. "Phase 1 T_ss (no UV)") for older results.json files
    that predate live tracking.
    """
    plateau_note = f"({'plateaued' if phase['converged'] else 'NOT fully plateaued'}, " \
                    f"{phase['iterations']} iterations)"
    span = phase.get("T_ss_window_span")
    if span is None:
        return [(f"Phase {phase_num} T_ss ({uv_note})", f"{phase['T_ss']:.4g} {plateau_note}")]
    cv = phase.get("T_ss_cv")
    return [
        (f"Phase {phase_num} moving average ({uv_note}, last {span:.4g} iterations)",
         f"{phase['T_ss']:.4g} {plateau_note}"),
        (f"Phase {phase_num} CV ({uv_note}, last {span:.4g} iterations)",
         f"{cv * 100:.1f}%" if cv is not None else "n/a"),
    ]

# Ventilation ACH is *measured* here (derived for free from Phase 1's own
# mass balance, no separate control run needed) instead of assumed at its
# nominal design value - see steady_state_pipeline.compute_corrected_eACH_uv's
# docstring. Only present when Phase 1/2 both produced a usable T_ss.
_ROW_LABELS_RESULTS_STEADY_STATE_MEASURED = [
    ("CFD measured Mechanical Ventilation ACH (determined from Phase 1)",
     lambda res: f"{res['ventilation_ach_measured']:.4g} /hr"),
    ("CFD measured eACH_uv",
     lambda res: f"{res['eACH_uv_steady_state_corrected']:.4g} /hr"),
]


def _total_ach_row(results):
    ach = results.get("ventilation_ach_measured")
    eACH_uv = results.get("eACH_uv_steady_state_corrected")
    if ach is None or eACH_uv is None:
        return "n/a"
    return f"{ach + eACH_uv:.4g} /hr"


def _monitoring_rows(monitoring):
    """Row list for monitoring locations, if any were computed. Handles both
    decay's shape ({name: {t_seconds, volAverage_T, eACH_uv_effective?}})
    and steady-state's shape ({name: {phase1: {...}, phase2: {...}}}).
    """
    if not monitoring:
        return []
    rows = []
    for name, data in monitoring.items():
        if "phase1" in data:
            p1, p2 = data["phase1"], data["phase2"]
            # T_ss/T_ss_cv (trailing-window moving average, see
            # decay_analysis.windowed_stats) when present; falls back to the
            # old last-sample read for results.json predating live tracking.
            T1 = p1.get("T_ss", p1["volAverage_T"][-1] if p1["volAverage_T"] else None)
            T2 = p2.get("T_ss", p2["volAverage_T"][-1] if p2["volAverage_T"] else None)
            value = f"T_ss1={T1:.4g}, T_ss2={T2:.4g}" if T1 is not None and T2 is not None else "n/a"
            cv1, cv2 = p1.get("T_ss_cv"), p2.get("T_ss_cv")
            if cv1 is not None or cv2 is not None:
                cv1_text = f"{cv1 * 100:.1f}%" if cv1 is not None else "n/a"
                cv2_text = f"{cv2 * 100:.1f}%" if cv2 is not None else "n/a"
                value += f" (CV1={cv1_text}, CV2={cv2_text})"
            if T1:
                value += f", reduction={(1 - T2 / T1) * 100:.1f}%"
        else:
            T_final = data["volAverage_T"][-1] if data["volAverage_T"] else None
            value = f"final volAverage(T)={T_final:.4g}" if T_final is not None else "n/a"
            if data.get("eACH_uv_effective") is not None:
                value += f", eACH_uv={data['eACH_uv_effective']:.4g}/hr"
        rows.append((name, value))
    return rows


def _room_setup_rows(room, settings):
    """_ROW_LABELS_ROOM's base rows, plus a 2nd inlet/outlet row (only when
    enabled), an injection-point row (steady-state runs only), and one row
    per monitoring point - all read straight from run_settings.json, same
    provenance as everything else in this table.
    """
    rows = [(label, fn(room, settings)) for label, fn in _ROW_LABELS_ROOM]
    if settings.get("inlet2-enable"):
        rows.append(("Inlet 2", f"{settings['inlet2-wall']}, y={settings['inlet2-y-input']:.3g}m "
                                 f"z={settings['inlet2-z-input']:.3g}m, size={settings['inlet2-size-w']:.3g}x"
                                 f"{settings['inlet2-size-h']:.3g}m"))
    if settings.get("outlet2-enable"):
        rows.append(("Outlet 2", f"{settings['outlet2-wall']}, y={settings['outlet2-y-input']:.3g}m "
                                  f"z={settings['outlet2-z-input']:.3g}m, size={settings['outlet2-size-w']:.3g}x"
                                  f"{settings['outlet2-size-h']:.3g}m"))
    source_center = settings.get("source_center")
    if source_center and all(v is not None for v in source_center):
        rows.append(("Injection point",
                      f"({source_center[0]:.3g}, {source_center[1]:.3g}, {source_center[2]:.3g}) m"))
    for pt in settings.get("monitoring_points") or []:
        rows.append((f"Monitoring point: {pt['name']}",
                      f"({pt['x']:.3g}, {pt['y']:.3g}, {pt['z']:.3g}) m, "
                      f"box={pt['cells_per_side']} cells/side"))
    return rows


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

    # injection_rate_total predates this field for case dirs from before it
    # was added to steady_state_pipeline.py - it's a deterministic function
    # of room volume/ACH/target_T_ss (see compute_source_strength), so an
    # older results.json missing it can still show the real number instead
    # of "n/a" rather than needing a rerun.
    if "phase1" in results and results.get("injection_rate_total") is None \
            and results.get("target_T_ss") and settings.get("ach"):
        results = dict(results)
        results["injection_rate_total"] = compute_source_strength(
            room.x * room.y * room.z, settings["ach"], results["target_T_ss"])

    # eACH_uv_well_mixed predates this field for steady-state case dirs from
    # before app.py's _run_steady_state started copying it out of setup_case()'s
    # summary - it's exactly Z * fluence_mean * 3.6 (see fluence.py's
    # compute_inactivation_rate/compute_well_mixed_eACH: linear in E, so
    # order of averaging doesn't matter), and fluence_mean has always been
    # saved, so an older results.json can still show the real number.
    if "phase1" in results and results.get("eACH_uv_well_mixed") is None \
            and results.get("fluence_mean") is not None and settings.get("z-value") is not None:
        results = dict(results)
        results["eACH_uv_well_mixed"] = settings["z-value"] * results["fluence_mean"] * 3.6

    fan_kwargs = {}
    if settings.get("fan-enable"):
        direction = (0, 0, -1) if settings["fan-direction"] == "down" else (0, 0, 1)
        fan_kwargs = dict(
            fan_speed=settings["fan-speed"], fan_disk_radius=settings["fan-radius"],
            fan_disk_thickness=settings["fan-thickness"],
            fan_center=(settings["fan-x-input"], settings["fan-y-input"], settings["fan-z-input"]),
            fan_direction=direction,
        )

    opening2_kwargs = {}
    if settings.get("inlet2-enable"):
        opening2_kwargs.update(
            inlet2_wall=settings["inlet2-wall"],
            inlet2_center=center_frac_for_wall(
                settings["inlet2-wall"], settings["inlet2-y-input"], settings["inlet2-z-input"], room),
            inlet2_size=(settings["inlet2-size-w"], settings["inlet2-size-h"]),
        )
    if settings.get("outlet2-enable"):
        opening2_kwargs.update(
            outlet2_wall=settings["outlet2-wall"],
            outlet2_center=center_frac_for_wall(
                settings["outlet2-wall"], settings["outlet2-y-input"], settings["outlet2-z-input"], room),
            outlet2_size=(settings["outlet2-size-w"], settings["outlet2-size-h"]),
        )

    source_center = settings.get("source_center")
    injection_center = (tuple(source_center)
                         if source_center and all(v is not None for v in source_center) else None)

    fig = plot_case(
        room,
        inlet_wall=settings["inlet-wall"],
        inlet_center=(settings["inlet-y-input"] / room.y, settings["inlet-z-input"] / room.z),
        inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
        outlet_wall=settings["outlet-wall"],
        outlet_center=(settings["outlet-y-input"] / room.y, settings["outlet-z-input"] / room.z),
        outlet_size=(settings["outlet-size-w"], settings["outlet-size-h"]),
        injection_center=injection_center,
        monitoring_points=settings.get("monitoring_points"),
        title="", **fan_kwargs, **opening2_kwargs,
    )
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), width=900, height=650)

    # Result curve (phase timeline for steady-state, decay curve for decay)
    # - only when the run actually recorded curve data, so older/minimal
    # case dirs still generate a report, just without this picture.
    if "phase1" in results:
        has_curve = bool(results["phase1"].get("decay_curve", {}).get("t")
                          and results["phase2"].get("decay_curve", {}).get("t"))
        curve_fig = steady_state_figure(results) if has_curve else None
    else:
        has_curve = bool(results.get("decay_curve", {}).get("t_seconds")
                          and results.get("eACH_uv_well_mixed") is not None)
        curve_fig = decay_figure(results) if has_curve else None
    if curve_fig is not None:
        curve_fig.update_layout(margin=dict(l=50, r=20, t=30, b=45), width=900, height=500)

    # Rendered pictures are staged in a real temp directory, not next to
    # out_path - if write_image() or doc.save() blows up partway through
    # (kaleido is known to be flaky - see test_report.py), nothing gets left
    # behind that could be mistaken for the report itself.
    with tempfile.TemporaryDirectory() as tmp_dir:
        image_path = Path(tmp_dir) / "preview.png"
        fig.write_image(str(image_path))
        curve_image_path = None
        if curve_fig is not None:
            curve_image_path = Path(tmp_dir) / "curve.png"
            curve_fig.write_image(str(curve_image_path))
        _write_report_docx(doc_out_path=out_path, case_dir=case_dir, guv_path=guv_path,
                            settings=settings, results=results, room=room,
                            image_path=image_path, curve_image_path=curve_image_path)
    return out_path


def _write_report_docx(doc_out_path, case_dir, guv_path, settings, results, room,
                        image_path, curve_image_path):
    doc = Document()
    doc.add_heading("GUV-CFD Simulation Report", level=1)
    doc.add_paragraph(f"Illuminate room design file: {guv_path}")
    doc.add_paragraph(f"CFD Project file: {settings.get('settings_path') or 'n/a'}")
    doc.add_paragraph(f"OpenFoam directory: {case_dir}")

    started_at, elapsed_seconds = _run_timing(case_dir, results)
    system_info = get_system_info()
    metadata_rows = []
    if started_at is not None:
        metadata_rows.append(("Simulation date", started_at.strftime("%Y-%m-%d %H:%M")))
    if elapsed_seconds is not None:
        metadata_rows.append(("Total elapsed time", _format_elapsed(elapsed_seconds)))
    metadata_rows.append(("CPU", system_info["cpu"]))
    if system_info["ram_gb"] is not None:
        metadata_rows.append(("RAM", f"{system_info['ram_gb']:.1f} GB"))
    if system_info["gpu"]:
        metadata_rows.append(("GPU", f"{system_info['gpu']} (not used - this simulation's "
                                      "OpenFOAM solve is CPU-only)"))
    _add_kv_table(doc, metadata_rows)

    doc.add_heading("Room Setup", level=2)
    _add_kv_table(doc, _room_setup_rows(room, settings))
    if settings.get("fan-enable"):
        _add_kv_table(doc, [(label, fn(settings)) for label, fn in _ROW_LABELS_FAN])

    doc.add_heading("Case Setup", level=2)
    doc.add_picture(str(image_path), width=Inches(6.0))

    doc.add_heading("Results", level=2)
    doc.add_paragraph().add_run(T_FIELD_NOTE).italic = True
    if "phase1" in results:
        rows = [(label, fn(results)) for label, fn in _ROW_LABELS_RESULTS_STEADY_STATE_BEFORE_PHASES]
        rows += _phase_ss_rows(1, "no UV", results["phase1"])
        rows += _phase_ss_rows(2, "UV on", results["phase2"])
        rows += [(label, fn(results)) for label, fn in _ROW_LABELS_RESULTS_STEADY_STATE_AFTER_PHASES]
        if results.get("ventilation_ach_measured") is not None:
            rows += [(label, fn(results)) for label, fn in _ROW_LABELS_RESULTS_STEADY_STATE_MEASURED]
        rows.append(("Total ACH in room (ACH+eACH_uv)", _total_ach_row(results)))
        _add_kv_table(doc, rows)
    else:
        _add_kv_table(doc, [(label, fn(results)) for label, fn in _ROW_LABELS_RESULTS_DECAY])
        if results.get("ventilation_ach_measured") is not None:
            _add_kv_table(doc, [(label, fn(results)) for label, fn in _ROW_LABELS_RESULTS_DECAY_CORRECTED])

    if curve_image_path is not None:
        doc.add_picture(str(curve_image_path), width=Inches(6.0))

    monitoring_rows = _monitoring_rows(results.get("monitoring"))
    if monitoring_rows:
        doc.add_heading("Monitoring Results", level=2)
        _add_kv_table(doc, monitoring_rows)

    uniformity_note = mixing_uniformity_note(results)
    if uniformity_note:
        doc.add_paragraph().add_run(uniformity_note).italic = True

    doc.save(doc_out_path)
