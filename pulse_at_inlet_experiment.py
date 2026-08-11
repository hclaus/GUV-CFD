#!/usr/bin/env python3
"""One-off experiment (see ANALYSIS_LOG.md, 2026-08-02 entry): does an Euler
decay run started from a pulse concentrated AT THE INLET - instead of the
standard uniform-everywhere initial condition - come closer to the
Lagrangian tracker's own residence-time behavior?

Clones the case's already-converged UV-off control run (same mesh, same
converged flow field, no UV source - a pure-ventilation comparison, matching
the earlier washout-fraction comparison), overwrites its T initial condition
with a sphere of T=1 centered at the inlet (T=0 elsewhere) instead of
`uniform 1`, reruns pimpleFoam, and compares both:

1. Room-average remaining fraction (volAverage(T) at t=500s) - by mass
   conservation (no UV, no other sink), this already equals "fraction of the
   pulse not yet exited," directly comparable to the Lagrangian washout
   fraction (1-F(500)) without needing any outlet measurement.
2. Outlet-average T over time (already computed as a side effect - see
   monitoring.write_vol_average_dict's default patches=("outlet",), unused
   by any reading code elsewhere in this codebase) - a real tracer
   breakthrough curve, the more direct analog of the Lagrangian RTD.

Usage:
    python pulse_at_inlet_experiment.py <case_dir> [--radius 0.5] [--end-time 500]
"""
import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

from guvcfd.case_io import read_cell_centers, read_patch_face_centers
from guvcfd.decay_analysis import write_results_summary
from guvcfd.initial_fields import boundary_field_block
from guvcfd.monitoring import write_vol_average_dict
from guvcfd.splice import set_control_dict_start_from, set_control_dict_time
from guvcfd.wsl_utils import run_wsl_or_raise, run_wsl_streaming, wsl_path


def pulse_T_field_content(cell_centers, inlet_center, radius, time_dir="0"):
    """A T field with internalField = sphere of 1.0 within `radius` of
    inlet_center, 0.0 elsewhere - a literal "pulse of contaminant right at
    the inlet," instead of write_initial_fields' usual `uniform 1`
    (room-wide). Same boundaryField as a normal T field (see
    initial_fields.boundary_field_block) - only the internal distribution
    changes.
    """
    dist = np.linalg.norm(cell_centers - np.asarray(inlet_center), axis=1)
    values = np.where(dist <= radius, 1.0, 0.0)
    n_pulsed = int(values.sum())

    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       volScalarField;", f'    location    "{time_dir}";',
        "    object      T;", "}", "",
        "dimensions      [0 0 0 0 0 0 0];", "",
        "internalField   nonuniform List<scalar> ", str(len(values)), "(",
    ]
    lines += [f"{v:.6g}" for v in values]
    lines += [")", ";", ""]
    content = "\n".join(lines) + "\n" + boundary_field_block("T", T_initial=1)
    return content, n_pulsed


def _read_surface_field_value(path):
    t, values = [], []
    with open(path) as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split()
            t.append(float(parts[0]))
            values.append(float(parts[1]))
    return t, values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_dir", help="Path to the case directory (containing a no_UV/ subfolder)")
    parser.add_argument("--radius", type=float, default=0.5, help="Pulse sphere radius [m] (default 0.5)")
    parser.add_argument("--end-time", type=float, default=500.0, help="Run duration [s] (default 500)")
    args = parser.parse_args()

    case_dir = Path(args.case_dir)
    no_uv_dir = case_dir / "no_UV"
    pulse_dir = case_dir / "no_UV_pulse"
    if not no_uv_dir.is_dir():
        print(f"ERROR: {no_uv_dir} not found - this case has no UV-off control run to clone.")
        sys.exit(1)

    no_uv_wsl = wsl_path(str(no_uv_dir))
    pulse_wsl = wsl_path(str(pulse_dir))

    print(f"=== Baseline (uniform IC, no UV) - reading existing {no_uv_dir}/results.json ===")
    with open(no_uv_dir / "results.json") as f:
        baseline = json.load(f)
    t_base = np.array(baseline["decay_curve"]["t_seconds"])
    T_base = np.array(baseline["decay_curve"]["volAverage_T"])
    idx = int(np.argmin(np.abs(t_base - args.end_time)))
    print(f"  Room-average T remaining at t={t_base[idx]:.0f}s: {T_base[idx]:.4f} "
          f"({100*(1-T_base[idx]):.1f}% reduction)")
    base_outlet_path = no_uv_dir / "postProcessing" / "outletAverage" / "0" / "surfaceFieldValue.dat"
    if base_outlet_path.exists():
        t_out_base, T_out_base = _read_surface_field_value(base_outlet_path)
        print(f"  (outlet-average curve also available: {len(t_out_base)} samples, "
              f"t=0..{t_out_base[-1]:.0f}s)")

    print(f"\n=== Cloning {no_uv_dir} -> {pulse_dir} ===")
    run_wsl_or_raise(f'rm -rf "{pulse_wsl}"', "$HOME", "clearing any stale pulse dir")
    run_wsl_or_raise(f'cp -r "{no_uv_wsl}" "{pulse_wsl}"', "$HOME", "cloning no_UV -> no_UV_pulse")
    run_wsl_or_raise(
        'for d in [0-9]*/; do [ "$d" = "0/" ] || rm -rf "$d"; done '
        '&& rm -rf postProcessing results.json log.pimpleFoam run_settings.json',
        pulse_wsl, "stripping the clone back to just its converged mesh/flow field",
    )

    print(f"\n=== Writing pulse T initial condition (radius={args.radius}m at the inlet) ===")
    cell_centers = read_cell_centers(str(pulse_dir), "0")
    inlet_faces = read_patch_face_centers(str(pulse_dir), "inlet")
    inlet_center = inlet_faces.mean(axis=0)
    content, n_pulsed = pulse_T_field_content(cell_centers, inlet_center, args.radius)
    with open(pulse_dir / "0" / "T", "w") as f:
        f.write(content)
    print(f"  Inlet center: {inlet_center}")
    print(f"  {n_pulsed} of {len(cell_centers)} cells set to T=1 ({100*n_pulsed/len(cell_centers):.1f}% of room volume)")

    print(f"\n=== Setting controlDict endTime={args.end_time}s ===")
    set_control_dict_start_from(str(pulse_dir), "startTime")
    set_control_dict_time(str(pulse_dir), end_time=args.end_time, write_interval=5)

    _time_re = re.compile(r"^Time\s*=\s*([\d.]+)")
    _last_printed = [-100.0]

    def _progress(line):
        m = _time_re.match(line.strip())
        if m:
            t = float(m.group(1))
            if t - _last_printed[0] >= 50:
                print(f"  {line}")
                _last_printed[0] = t

    print("\n=== Running pimpleFoam ===")
    r = run_wsl_streaming("pimpleFoam 2>&1 | tee log.pimpleFoam", pulse_wsl,
                           on_line=_progress, kill_pattern="pimpleFoam")
    if r.returncode != 0 or "FOAM FATAL" in r.stdout:
        print("FAILED. Tail of solver output:")
        print("\n".join(r.stdout.splitlines()[-30:]))
        sys.exit(1)

    print("\n=== Post-processing (room + outlet volAverage) ===")
    write_vol_average_dict(str(pulse_dir))
    run_wsl_or_raise("rm -rf postProcessing", pulse_wsl, "clearing postProcessing")
    run_wsl_or_raise("postProcess -dict system/volAverageDict", pulse_wsl, "postProcess volAverage")
    results = write_results_summary(str(pulse_dir), f"{pulse_dir}/results.json", baseline["ventilation_ach"], 0.0)

    t_pulse = np.array(results["decay_curve"]["t_seconds"])
    T_pulse = np.array(results["decay_curve"]["volAverage_T"])
    idx_p = int(np.argmin(np.abs(t_pulse - args.end_time)))

    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"{'Scenario':<45}{'remaining':>12}{'reduction %':>14}")
    print(f"{'Lagrangian washout (rigorous, N=500)':<45}{0.412:>12.3f}{58.8:>14.1f}")
    print(f"{'Euler, uniform IC (existing baseline)':<45}{T_base[idx]:>12.3f}{100*(1-T_base[idx]):>14.1f}")
    print(f"{'Euler, pulse-at-inlet IC (NEW)':<45}{T_pulse[idx_p]:>12.3f}{100*(1-T_pulse[idx_p]):>14.1f}")

    pulse_outlet_path = pulse_dir / "postProcessing" / "outletAverage" / "0" / "surfaceFieldValue.dat"
    if pulse_outlet_path.exists():
        t_out_pulse, T_out_pulse = _read_surface_field_value(pulse_outlet_path)
        print(f"\nOutlet-average breakthrough curve written: {pulse_outlet_path}")
        print(f"  ({len(t_out_pulse)} samples, t=0..{t_out_pulse[-1]:.0f}s)")

    print(f"\nFull results: {pulse_dir}/results.json")


if __name__ == "__main__":
    main()
