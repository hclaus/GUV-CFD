#!/usr/bin/env python3
"""Three-way comparison of methods for predicting UV inactivation in a
finished CFD case:

1. Euler / decay-curve (established): the room's real, directly-simulated
   volume-averaged contaminant decay (ventilation + UV combined) from
   results.json - a one-time-contamination scenario (room starts fully
   contaminated, decays over elapsed time). This is the trusted,
   already-validated production method - nothing here recomputes it.

2. Age-field snapshot (superseded - kept only for comparison): dose[cell]
   = fluenceRate[cell] * age[cell], volume-averaged across all cells. A
   continuous-source, single-pass survival estimate, but a flawed one -
   see guvcfd/lagrangian_tracking.py's module docstring for why "dose so
   far at a snapshot point" isn't the same as "dose accumulated by the
   time a parcel actually exits."

3. Lagrangian particle tracking (new, most rigorous): particles seeded at
   the inlet (flux-weighted), each integrated through the real solved
   velocity field until it exits, accumulating true path-integrated dose.
   Includes turbulent dispersion (a random walk scaled by the solved nut
   field) on top of mean-flow advection - without it, particles trapped
   in near-stagnant recirculation zones get excluded from the result
   entirely (a survivorship bias - see lagrangian_tracking.integrate_particles'
   `diffuse` docstring). Also a continuous-source, single-pass survival
   estimate - directly comparable to method 2, NOT to method 1 (different
   physical scenario: continuous steady operation vs.
   one-time decay-after-contamination).

Usage:
    python compare_dose_methods.py                (prompts for case dir)
    python compare_dose_methods.py /path/to/case   (skips the prompt)
"""
import json
import sys
import time
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

from guvcfd.age_analysis import ashrae_air_change_effectiveness, build_rtd_histogram, read_age_field, rtd_from_histogram
from guvcfd.case_io import read_cell_centers, read_openfoam_scalar_field
from guvcfd.dose_distribution import compute_dose_at_cells
from guvcfd.lagrangian_tracking import run_lagrangian_tracking


def get_case_dir():
    if len(sys.argv) > 1:
        case = Path(sys.argv[1])
        if case.is_dir():
            return case
        print(f"ERROR: Directory not found: {sys.argv[1]}")
        sys.exit(1)
    print("\n" + "=" * 70)
    print("THREE-WAY DOSE METHOD COMPARISON")
    print("=" * 70 + "\n")
    while True:
        raw = input("Enter case directory path (or 'q' to quit): ").strip()
        if raw.lower() == "q":
            sys.exit(0)
        case = Path(raw)
        if case.is_dir():
            return case
        print(f"ERROR: Directory not found: {raw}\nTry again.\n")


def get_float(prompt, default=None):
    while True:
        raw = input(prompt).strip()
        if not raw and default is not None:
            return float(default)
        try:
            return float(raw)
        except ValueError:
            print("Invalid number, try again.")


def get_int(prompt, default):
    raw = input(prompt).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"Invalid number - using default {default}.")
        return default


def method1_decay_curve(case_dir):
    """Method 1: read the already-simulated room decay curve directly -
    no recomputation, this is the established/trusted result."""
    results_path = case_dir / "results.json"
    if not results_path.exists():
        return None
    with open(results_path) as f:
        results = json.load(f)
    decay_curve = results.get("decay_curve")
    if not decay_curve:
        return None
    t = np.asarray(decay_curve["t_seconds"])
    T = np.asarray(decay_curve["volAverage_T"])
    return {
        "eACH_uv_effective": results.get("eACH_uv_effective"),
        "ventilation_ach": results.get("ventilation_ach"),
        "t_final": float(t[-1]),
        "N_over_N0_at_t_final": float(T[-1]),
        "log_reduction_at_t_final": -np.log10(max(float(T[-1]), 1e-12)),
    }


def method2_age_snapshot(case_dir, Z):
    """Method 2: the (known-flawed) age-field snapshot dose estimate -
    see module docstring. Mirrors tracer_dose_report.py's calculation."""
    cell_centers = read_cell_centers(case_dir, "0")
    fluence = np.asarray(read_openfoam_scalar_field(case_dir / "0" / "fluenceRate"), dtype=float)
    age = np.asarray(read_age_field(case_dir), dtype=float)
    age = np.clip(age, 0, None)  # see tracer_dose_report.py: linearUpwind isn't perfectly monotone

    dose = compute_dose_at_cells(fluence, age)
    N_over_N0 = float(np.mean(np.exp(-Z * dose)))  # direct per-cell average, no binning error
    mean_age = float(age.mean())

    theta_edges, theta_counts = build_rtd_histogram(age / mean_age, n_bins=50)
    theta_centers, E_theta = rtd_from_histogram(theta_edges, theta_counts)

    return {
        "mean_age": mean_age,
        "mean_dose": float(dose.mean()),
        "N_over_N0": N_over_N0,
        "log_reduction": -np.log10(max(N_over_N0, 1e-12)),
        "theta_centers": theta_centers,
        "E_theta": E_theta,
    }


def method3_lagrangian(case_dir, Z, n_particles, seed=0):
    """Method 3: true full-trajectory dose via particle tracking."""
    t0 = time.time()
    result = run_lagrangian_tracking(case_dir, n_particles=n_particles, seed=seed)
    elapsed = time.time() - t0

    exited = result["exited"]
    n_exited = int(exited.sum())
    if n_exited == 0:
        raise RuntimeError("No particles exited within the time cap - can't compute a dose distribution.")
    dose_exit = result["dose"][exited]
    t_exit = result["t_exit"][exited]

    N_over_N0 = float(np.mean(np.exp(-Z * dose_exit)))
    mean_age = float(t_exit.mean())

    # Far fewer samples than the age-field method's ~40k cells - scale bin
    # count down accordingly (sqrt-of-N heuristic) or the histogram is
    # mostly empty/spiky bins rather than a readable distribution shape.
    n_bins = max(5, min(50, int(np.sqrt(n_exited) * 2)))
    theta_edges, theta_counts = build_rtd_histogram(t_exit / mean_age, n_bins=n_bins)
    theta_centers, E_theta = rtd_from_histogram(theta_edges, theta_counts)

    return {
        "n_particles": n_particles,
        "n_exited": n_exited,
        "n_trapped": n_particles - n_exited,
        "elapsed_seconds": elapsed,
        "mean_residence_time": mean_age,
        "mean_dose": float(dose_exit.mean()),
        "N_over_N0": N_over_N0,
        "log_reduction": -np.log10(max(N_over_N0, 1e-12)),
        "theta_centers": theta_centers,
        "E_theta": E_theta,
    }


def make_comparison_plot(case_name, Z, m2, m3, plot_path):
    fig, ax = plt.subplots(figsize=(8, 6))
    ax.plot(m2["theta_centers"], m2["E_theta"], label="Age-field snapshot (flawed)", color="tab:red")
    ax.plot(m3["theta_centers"], m3["E_theta"], label="Lagrangian tracking (rigorous)", color="tab:blue")
    theta_ideal = np.linspace(0, 3.0, 200)
    ax.plot(theta_ideal, np.exp(-theta_ideal), "--", color="gray", label="Ideal CSTR (perfect mixing)")
    ax.axvline(1.0, color="gray", linestyle=":", alpha=0.5)
    ax.set_xlabel(r"Normalized residence time $\theta$ = age (or exit time) / mean")
    ax.set_ylabel(r"E($\theta$)  [density]")
    ax.set_title(f"{case_name} - RTD comparison, age-field vs. Lagrangian tracking")
    ax.legend()
    fig.tight_layout()
    fig.savefig(plot_path, dpi=150)
    return fig


def main():
    case_dir = get_case_dir()
    case_name = case_dir.name

    settings_path = case_dir / "run_settings.json"
    default_Z = None
    if settings_path.exists():
        try:
            default_Z = json.loads(settings_path.read_text()).get("z-value")
        except (OSError, ValueError):
            pass

    Z = get_float(f"UV sensitivity Z [cm²/mJ]{f' (Enter for {default_Z})' if default_Z is not None else ''}: ",
                   default_Z)
    print("\nNote: turbulent dispersion (diffuse=True, the default) makes particle tracking roughly")
    print("linear-cost in particle count - empirically ~10-16s/particle on a room-sized case, so a")
    print("few hundred particles can take an hour or more. Start small if you're not sure yet.")
    n_particles = get_int(
        "Lagrangian particle count (more = smoother distribution but slower; Enter for 100): ", 100)

    print("\n" + "-" * 70)
    print("METHOD 1: Euler / decay-curve (established, from results.json)")
    print("-" * 70)
    m1 = method1_decay_curve(case_dir)
    if m1 is None:
        print("  No results.json / decay_curve found - skipping.")
    else:
        print(f"  eACH_uv_effective:        {m1['eACH_uv_effective']:.3g} /hr")
        print(f"  N/N0 at t={m1['t_final']:.0f}s (actual decay curve): {m1['N_over_N0_at_t_final']:.4e} "
              f"(log reduction {m1['log_reduction_at_t_final']:.2f})")
        print("  Scenario: room starts fully contaminated, decays over elapsed time")
        print("  (ventilation + UV combined) - NOT directly comparable to methods 2/3 below,")
        print("  which model a continuously-operating room's single-pass survival instead.")

    print("\n" + "-" * 70)
    print("METHOD 2: Age-field snapshot dose (superseded, kept for comparison)")
    print("-" * 70)
    m2 = method2_age_snapshot(case_dir, Z)
    print(f"  Mean age:    {m2['mean_age']:.2f} s")
    print(f"  Mean dose:   {m2['mean_dose']:.4g} mJ/cm²")
    print(f"  N/N0:        {m2['N_over_N0']:.4e}  (log reduction {m2['log_reduction']:.2f})")

    print("\n" + "-" * 70)
    print(f"METHOD 3: Lagrangian particle tracking ({n_particles} particles - this can take a while)")
    print("-" * 70)
    m3 = method3_lagrangian(case_dir, Z, n_particles)
    print(f"  Ran in {m3['elapsed_seconds']:.1f}s - {m3['n_exited']}/{n_particles} particles exited, "
          f"{m3['n_trapped']} trapped at the time cap")
    print(f"  Mean residence time (exited only): {m3['mean_residence_time']:.2f} s")
    print(f"  Mean dose (exited only):           {m3['mean_dose']:.4g} mJ/cm²")
    print(f"  N/N0:        {m3['N_over_N0']:.4e}  (log reduction {m3['log_reduction']:.2f})")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"{'Method':<45}{'N/N0':>14}{'log10 reduction':>18}")
    if m1 is not None:
        print(f"{'1. Euler decay curve (elapsed-time scenario)':<45}{m1['N_over_N0_at_t_final']:>14.4e}"
              f"{m1['log_reduction_at_t_final']:>18.2f}")
    print(f"{'2. Age-field snapshot (single-pass, flawed)':<45}{m2['N_over_N0']:>14.4e}{m2['log_reduction']:>18.2f}")
    print(f"{'3. Lagrangian tracking (single-pass, rigorous)':<45}{m3['N_over_N0']:>14.4e}{m3['log_reduction']:>18.2f}")
    if m3["N_over_N0"] > 0:
        ratio = m2["N_over_N0"] / m3["N_over_N0"]  # age-snapshot survival relative to Lagrangian survival
        direction = "OVER-predicts survival (under-predicts kill)" if ratio > 1 else \
            "UNDER-predicts survival (over-predicts kill)"
        print("\nMethod 2 vs Method 3 (both single-pass - this comparison IS apples-to-apples):")
        print(f"  Age-snapshot N/N0 is {ratio:.2f}x the Lagrangian N/N0.")
        print(f"  The age-field snapshot method {direction} by ~{abs(ratio - 1) * 100:.0f}% "
              "relative to the rigorous Lagrangian result.")

    plot_path = case_dir / f"{case_name}_dose_method_comparison.png"
    make_comparison_plot(case_name, Z, m2, m3, plot_path)
    print(f"\nComparison plot written: {plot_path}")

    try:
        plt.show()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nCancelled by user.")
        sys.exit(0)
    input("\nPress Enter to exit...")
