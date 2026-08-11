#!/usr/bin/env python3
"""Interactive tracer + dose report: per-cell age (residence time) and UV
dose from a finished CFD case, exported as CSV tables plus a plot of the
normalized residence time distribution and the dose distribution.

Reads fluenceRate, age, and cell centers directly from the case's own
files - fluenceRate is already computed from the real lamp geometry and
written to disk during normal case setup (run_pipeline._finish_case_setup),
so this script needs no guv_calcs / .guv project reload at all.

Usage:
    python tracer_dose_report.py                  (prompts for case dir)
    python tracer_dose_report.py /path/to/case     (skips the prompt)

Dose physics (see guvcfd/dose_distribution.py):
    dose[cell] = fluenceRate[cell] [uW/cm^2] * age[cell] [s] * 1e-3  ->  mJ/cm^2
    N/N0 = exp(-Z * dose), Z [cm^2/mJ] applied directly per Blatchley's
    segregated-flow model (dose already combines fluence and residence
    time, so Z is used as-is - not re-derived into a time-rate constant).
"""
import csv
import json
import sys
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt

# Windows consoles default to a codepage that mangles the ² and other
# unicode characters used in the prompts/output below (e.g. "cm²" -> "cm�").
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

from guvcfd.age_analysis import (
    ashrae_air_change_effectiveness, build_rtd_histogram, read_age_field, rtd_from_histogram,
)
from guvcfd.case_io import latest_time_dir, read_cell_centers, read_openfoam_scalar_field
from guvcfd.dose_distribution import compute_dose_at_cells


def get_case_dir():
    if len(sys.argv) > 1:
        case = Path(sys.argv[1])
        if case.is_dir():
            return case
        print(f"ERROR: Directory not found: {sys.argv[1]}")
        sys.exit(1)

    print("\n" + "=" * 70)
    print("TRACER DOSE REPORT - Residence Time + UV Dose Distribution")
    print("=" * 70 + "\n")
    while True:
        raw = input("Enter case directory path (or 'q' to quit): ").strip()
        if raw.lower() == "q":
            print("Cancelled.")
            sys.exit(0)
        case = Path(raw)
        if case.is_dir():
            return case
        print(f"ERROR: Directory not found: {raw}\nTry again.\n")


def load_run_settings(case_dir):
    path = case_dir / "run_settings.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def get_float(prompt, default=None):
    while True:
        raw = input(prompt).strip()
        if not raw and default is not None:
            return float(default)
        try:
            return float(raw)
        except ValueError:
            print("Invalid number, try again.")


def get_optional_float(prompt):
    raw = input(prompt).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        print("Invalid number - skipping.")
        return None


def read_fluence_rate(case_dir):
    path = case_dir / "0" / "fluenceRate"
    if not path.exists():
        raise RuntimeError(
            f"{path} not found - this case doesn't look like it went through normal case setup "
            "(fluenceRate is written automatically at that point). Run the case through the app first."
        )
    return np.asarray(read_openfoam_scalar_field(path), dtype=float)


def log_binned_distribution(values, n_bins=50):
    """Density histogram (bin_centers, density) using log-spaced bins over
    the positive values in `values` - unlike a fixed-width linear
    histogram, this resolves both a right-skewed field's dense low-value
    region AND its long tail (see segregated_flow_inactivation's
    docstring for why a linear histogram badly under-resolves dose
    fields like this one). Exact zeros (e.g. clipped negative-age cells)
    are excluded from the returned bins - they don't have a meaningful
    place on a log axis, and are reported separately by the caller.

    bin_centers use the geometric mean of each bin's edges (the natural
    "center" on a log axis, unlike the arithmetic mean a linear histogram
    would use).
    """
    positive = values[values > 0]
    if positive.size == 0 or positive.min() == positive.max():
        return None, None
    edges = np.logspace(np.log10(positive.min()), np.log10(positive.max()), n_bins + 1)
    counts, edges = np.histogram(positive, bins=edges)
    centers = np.sqrt(edges[:-1] * edges[1:])
    widths = np.diff(edges)
    density = counts / (counts.sum() * widths)
    return centers, density


def write_percell_table(path, cell_centers, age, normalized_age, fluence, dose):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["x_m", "y_m", "z_m", "age_s", "age_normalized", "fluence_uW_cm2", "dose_mJ_cm2"])
        for (x, y, z), a, an, fl, d in zip(cell_centers, age, normalized_age, fluence, dose):
            writer.writerow([f"{x:.6g}", f"{y:.6g}", f"{z:.6g}", f"{a:.6g}", f"{an:.6g}", f"{fl:.6g}", f"{d:.6g}"])


def write_distribution_table(path, theta_centers, E_theta, dose_centers, E_D):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["bin_type", "bin_center", "density"])
        for c, e in zip(theta_centers, E_theta):
            writer.writerow(["normalized_age_theta", f"{c:.6g}", f"{e:.6g}"])
        for c, e in zip(dose_centers, E_D):
            writer.writerow(["dose_mJ_cm2", f"{c:.6g}", f"{e:.6g}"])


def make_plot(case_name, Z, theta_centers, E_theta, dose_centers, E_D, mean_dose, plot_path):
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))

    axes[0].plot(theta_centers, E_theta, label="CFD (this room)")
    theta_ideal = np.linspace(0, max(theta_centers.max(), 3.0), 200)
    axes[0].plot(theta_ideal, np.exp(-theta_ideal), "--", color="gray",
                 label="Ideal CSTR (perfect mixing)")
    axes[0].axvline(1.0, color="gray", linestyle=":", label=r"$\theta$=1 (mean)")
    axes[0].set_xlabel(r"Normalized residence time $\theta$ = age / mean(age)")
    axes[0].set_ylabel(r"E($\theta$)  [density]")
    axes[0].set_title("Normalized Residence Time Distribution")
    axes[0].legend()

    axes[1].plot(dose_centers, E_D, color="tab:orange")
    axes[1].axvline(mean_dose, color="gray", linestyle=":", label=f"mean = {mean_dose:.4g} mJ/cm²")
    axes[1].set_xscale("log")
    axes[1].set_xlabel(r"Dose D [mJ/cm²]  (log scale)")
    axes[1].set_ylabel("E(D)  [density]")
    axes[1].set_title("UV Dose Distribution")
    axes[1].legend()

    fig.suptitle(f"{case_name}  —  Z = {Z:g} cm²/mJ")
    fig.tight_layout()
    fig.savefig(plot_path, dpi=150)
    return fig


def main():
    case_dir = get_case_dir()
    case_name = case_dir.name
    settings = load_run_settings(case_dir)

    default_Z = settings.get("z-value")
    default_ach = settings.get("ach")
    if settings:
        print(f"\nFound run_settings.json: ACH={default_ach}, Z={default_Z}")
    else:
        print("\nNo run_settings.json found in this case - enter parameters manually.")

    print("\n" + "-" * 70)
    Z = get_float(
        f"UV sensitivity Z [cm²/mJ]{f' (Enter for {default_Z})' if default_Z is not None else ''}: ",
        default_Z,
    )
    ach = get_optional_float(
        f"Nominal ACH [1/hr] for ASHRAE effectiveness{f' (Enter for {default_ach})' if default_ach is not None else ' (Enter to skip)'}: "
    ) or default_ach

    print("\nReading cell centers, fluence rate, and age field...")
    cell_centers = read_cell_centers(case_dir, "0")
    fluence = read_fluence_rate(case_dir)
    age_time = latest_time_dir(case_dir)
    age = np.asarray(read_age_field(case_dir), dtype=float)

    n = len(cell_centers)
    if not (len(fluence) == n and len(age) == n):
        raise RuntimeError(
            f"Field length mismatch: {n} cell centers, {len(fluence)} fluenceRate, {len(age)} age - "
            "the mesh may have changed between time 0 and the latest time directory."
        )
    print(f"  {n} cells, age field read from t={age_time}s")

    # age is physically >= 0 (it's a time-since-entering field, fixedValue=0
    # at inlets) - the "bounded Gauss linearUpwind" convection scheme isn't
    # perfectly monotone, so a few cells near sharp gradients can end up
    # very slightly negative (numerical overshoot, not a real result).
    # Clip and flag rather than let a handful of bad cells distort the
    # normalized-age/dose distributions below.
    n_negative = int(np.sum(age < 0))
    if n_negative:
        print(f"  WARNING: {n_negative} of {n} cells have negative age "
              f"(min {age.min():.3g}s) - numerical overshoot, clipping to 0")
        age = np.clip(age, 0, None)

    dose = compute_dose_at_cells(fluence, age)  # mJ/cm^2
    mean_age = float(age.mean())
    mean_dose = float(dose.mean())
    normalized_age = age / mean_age

    print("\n" + "-" * 70)
    print("RESIDENCE TIME (AGE)")
    print(f"  Mean age:   {mean_age:.2f} s")
    print(f"  Std dev:    {age.std():.2f} s")
    print(f"  Min / Max:  {age.min():.2f} s / {age.max():.2f} s")

    if ach:
        mixing = ashrae_air_change_effectiveness(age, ach)
        print(f"\nASHRAE AIR-CHANGE EFFECTIVENESS (target {ach:g} ACH)")
        print(f"  Effective ACH:  {mixing['effective_ach']:.2f} 1/hr")
        print(f"  Effectiveness:  {100 * mixing['effectiveness']:.1f}%")

    print("\n" + "-" * 70)
    print("UV DOSE")
    print(f"  Mean dose:  {mean_dose:.4g} mJ/cm²")
    print(f"  Std dev:    {dose.std():.4g} mJ/cm²")
    print(f"  Min / Max:  {dose.min():.4g} / {dose.max():.4g} mJ/cm²")

    # Distributions (for the plot/table only - see below for why the
    # inactivation prediction itself does NOT use these bins)
    theta_edges, theta_counts = build_rtd_histogram(normalized_age, n_bins=50)
    theta_centers, E_theta = rtd_from_histogram(theta_edges, theta_counts)

    # Dose is heavily right-skewed (most cells near D~0, long tail) - a
    # linear histogram collapses the interesting low-dose region into one
    # bin, so use log-spaced bins instead (see log_binned_distribution).
    dose_centers, E_D = log_binned_distribution(dose, n_bins=50)
    if dose_centers is None:
        print("  WARNING: dose field has no variation - skipping dose distribution plot/table")
        dose_centers, E_D = np.array([mean_dose]), np.array([1.0])

    # segregated_flow_inactivation's 50-bin histogram integral is too coarse
    # for a dose field this right-skewed (most cells cluster near D~0-3
    # mJ/cm^2 with a long tail to 100+, so a handful of wide linear bins
    # collapse the entire low-dose region - exactly where exp(-Z*D) varies
    # fastest - into one bin, systematically OVER-predicting kill).
    # Average exp(-Z*D) directly over every cell's own dose instead - no
    # binning error, and it's cheap since we already have the full per-cell
    # dose array. Confirmed this matters: for this case the binned estimate
    # violated Jensen's inequality (predicted MORE kill than the well-mixed
    # approximation, which is impossible for a convex function like
    # exp(-Z*D) - heterogeneous dose can only ever predict less or equal
    # kill than applying the mean dose everywhere).
    N_over_N0 = float(np.mean(np.exp(-Z * dose)))
    log_reduction = -np.log10(max(N_over_N0, 1e-12))
    N_over_N0_mixed = np.exp(-Z * mean_dose)
    log_reduction_mixed = -np.log10(max(N_over_N0_mixed, 1e-12))

    print("\n" + "-" * 70)
    print("SEGREGATED-FLOW INACTIVATION PREDICTION")
    print(f"  N/N0 (dose distribution):  {N_over_N0:.4e}  (log reduction {log_reduction:.2f})")
    print(f"  N/N0 (well-mixed, mean dose only): {N_over_N0_mixed:.4e}  (log reduction {log_reduction_mixed:.2f})")
    if N_over_N0_mixed > 0:
        ratio = N_over_N0 / N_over_N0_mixed
        print(f"  Ratio dose-distribution / well-mixed: {ratio:.2f}x"
              + ("  -> dose non-uniformity matters here" if ratio > 1.5 or ratio < 0.67
                 else "  -> well-mixed approximation is reasonable"))

    # Exports
    table_path = case_dir / f"{case_name}_tracer_dose_percell.csv"
    write_percell_table(table_path, cell_centers, age, normalized_age, fluence, dose)

    dist_path = case_dir / f"{case_name}_tracer_dose_distributions.csv"
    write_distribution_table(dist_path, theta_centers, E_theta, dose_centers, E_D)

    plot_path = case_dir / f"{case_name}_tracer_dose_plot.png"
    make_plot(case_name, Z, theta_centers, E_theta, dose_centers, E_D, mean_dose, plot_path)

    print("\n" + "=" * 70)
    print("FILES WRITTEN")
    print(f"  Per-cell table:    {table_path}")
    print(f"  Distribution table: {dist_path}")
    print(f"  Plot:              {plot_path}")
    print("=" * 70 + "\n")

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
