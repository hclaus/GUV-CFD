#!/usr/bin/env python3
"""
Demo script: extract RTD and dose distribution from an OpenFOAM case
with the new age-of-air tracer field.

Usage:
    python tracer_demo.py <case_dir> [--ach ACH] [--volume VOLUME] [--Z Z]

Example:
    python tracer_demo.py /path/to/case --ach 6 --volume 30 --Z 6

Note: Z is the sensitivity parameter [cm²/mJ]; k is computed as k = Z * E_avg * 1e-3.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

from guvcfd.age_analysis import (
    read_age_field, compute_age_statistics, rtd_from_age_field,
    ashrae_air_change_effectiveness
)
from guvcfd.dose_distribution import (
    compute_dose_at_cells, build_dose_distribution,
    dose_distribution_function, segregated_flow_inactivation
)
from guvcfd.fluence import compute_fluence_at_points
from guvcfd.case_io import read_cell_centers, latest_time_dir


def demo_rtd_extraction(case_dir, target_ach=None, room_volume=None):
    """Extract and display RTD from age field."""
    print(f"\n{'='*60}")
    print("TRACER ANALYSIS: Age-of-Air Field → Residence Time Distribution")
    print(f"{'='*60}\n")

    try:
        age_values = read_age_field(case_dir)
    except Exception as e:
        print(f"ERROR: Could not read age field from {case_dir}")
        print(f"  {e}")
        print("  (Has the case been run with the new age-of-air solver?)")
        return False

    print(f"Read {len(age_values)} cell values from age field\n")

    # Age statistics
    stats = compute_age_statistics(age_values)
    print("Age Field Statistics:")
    print(f"  Mean age:     {stats['mean_age']:.2f} s")
    print(f"  Std dev:      {stats['std_age']:.2f} s")
    print(f"  Min / Max:    {stats['min_age']:.2f} s / {stats['max_age']:.2f} s\n")

    # RTD extraction
    rtd = rtd_from_age_field(age_values, n_bins=50)
    print(f"Residence Time Distribution (50 bins):")
    print(f"  ∫ E(t) dt     ≈ {np.trapz(rtd['E_t'], rtd['bin_centers']):.4f}")
    print(f"  Peak E(t):    {rtd['E_t'].max():.4e} s⁻¹\n")

    # ASHRAE effectiveness (if ACH and volume given)
    if target_ach is not None and room_volume is not None:
        mixing = ashrae_air_change_effectiveness(age_values, target_ach, room_volume)
        print(f"ASHRAE Air-Change Effectiveness (target {target_ach} ACH):")
        print(f"  Effective ACH: {mixing['effective_ach']:.2f} 1/hr")
        print(f"  Effectiveness: ε_a = {mixing['effectiveness']:.2%}")
        print(f"    → {100 * mixing['effectiveness']:.1f}% of nominal mixing achieved\n")

    return age_values


def demo_dose_distribution(case_dir, age_values, Z=6.0):
    """Compute and display dose distribution and inactivation.

    Z: sensitivity parameter [cm²/mJ] - k is computed as k = Z * E_avg * 1e-3.
    """
    print(f"{'='*60}")
    print("DOSE DISTRIBUTION & SEGREGATED-FLOW INACTIVATION")
    print(f"{'='*60}\n")

    try:
        # Read cell geometry
        time_dir = latest_time_dir(case_dir)
        cell_centers = read_cell_centers(case_dir, time_dir)
    except Exception as e:
        print(f"WARNING: Could not read cell centers: {e}")
        print("  Skipping dose distribution (requires cell center data)")
        return None

    try:
        fluence_rate = compute_fluence_at_points(case_dir, cell_centers)
    except Exception as e:
        print(f"WARNING: Could not compute fluence: {e}")
        print("  (Make sure lamp geometry is configured)")
        return None

    # Compute dose
    dose_values = compute_dose_at_cells(fluence_rate, age_values)
    print(f"Computed {len(dose_values)} cell doses from fluence × age")

    # Compute k from Z and mean fluence
    mean_fluence_uW_cm2 = np.mean(fluence_rate)
    k = Z * mean_fluence_uW_cm2 * 1e-3  # k [1/s] = Z [cm²/mJ] * E [uW/cm²] * 1e-3
    print(f"  Mean fluence rate:  {mean_fluence_uW_cm2:.2f} uW/cm²")
    print(f"  Sensitivity Z:      {Z} cm²/mJ")
    print(f"  Computed k:         {k:.4f} 1/s\n")

    # Dose statistics
    dose_stats = {
        "mean": np.mean(dose_values),
        "std": np.std(dose_values),
        "min": np.min(dose_values),
        "max": np.max(dose_values),
    }
    print("Dose Statistics:")
    print(f"  Mean dose:    {dose_stats['mean']:.4f} mJ/cm²")
    print(f"  Std dev:      {dose_stats['std']:.4f} mJ/cm²")
    print(f"  Min / Max:    {dose_stats['min']:.4f} / {dose_stats['max']:.4f} mJ/cm²\n")

    # Dose distribution
    bin_edges, bin_counts = build_dose_distribution(dose_values, n_bins=50)
    bin_centers, E_D = dose_distribution_function(bin_edges, bin_counts)

    print(f"Dose Distribution (50 bins):")
    print(f"  ∫ E(D) dD     ≈ {np.trapz(E_D, bin_centers):.4f}\n")

    # Segregated-flow inactivation prediction
    N_over_N0 = segregated_flow_inactivation(bin_centers, E_D, k)
    log_reduction = -np.log10(max(N_over_N0, 1e-10))

    print(f"Segregated-Flow Model Inactivation Prediction:")
    print(f"  Pathogen Z:   {Z} cm²/mJ")
    print(f"  Computed k:   {k:.4f} 1/s")
    print(f"  N/N₀:         {N_over_N0:.4e}")
    print(f"  Log reduction: {log_reduction:.2f} (i.e., 10^{-log_reduction:.1f} kill)")
    print(f"  Survival:     {100 * N_over_N0:.2%}\n")

    # Comparison: well-mixed approximation (average dose only)
    mean_dose = np.mean(dose_values)
    N_over_N0_mixed = np.exp(-k * mean_dose)
    log_reduction_mixed = -np.log10(max(N_over_N0_mixed, 1e-10))

    print(f"Well-Mixed Approximation (average dose only):")
    print(f"  N/N₀:         {N_over_N0_mixed:.4e}")
    print(f"  Log reduction: {log_reduction_mixed:.2f}\n")

    # Divergence
    if N_over_N0_mixed > 0:
        divergence_ratio = N_over_N0 / N_over_N0_mixed
        print(f"Divergence (dose-distribution vs. well-mixed):")
        print(f"  Ratio N/N₀ᵈⁱˢᵗ / N/N₀ʷᵐ: {divergence_ratio:.2f}x")
        if divergence_ratio > 1.5:
            print(f"  → Dose distribution SIGNIFICANTLY changes prediction\n")
        else:
            print(f"  → Well-mixed approximation reasonably accurate\n")

    return {
        "dose_values": dose_values,
        "dose_statistics": dose_stats,
        "E_D": E_D,
        "bin_centers": bin_centers,
        "N_over_N0": N_over_N0,
        "log_reduction": log_reduction,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Extract RTD and dose distribution from CFD case with age-of-air field"
    )
    parser.add_argument("case_dir", help="Path to OpenFOAM case directory")
    parser.add_argument("--ach", type=float, default=None,
                        help="Nominal ventilation ACH [1/hr] (for effectiveness calculation)")
    parser.add_argument("--volume", type=float, default=None,
                        help="Room volume [m³] (for effectiveness calculation)")
    parser.add_argument("--Z", type=float, default=6.0,
                        help="Sensitivity parameter [cm²/mJ] (default: 6)")
    args = parser.parse_args()

    case_path = Path(args.case_dir)
    if not case_path.is_dir():
        print(f"ERROR: Case directory not found: {args.case_dir}")
        sys.exit(1)

    # RTD extraction
    age_values = demo_rtd_extraction(case_path, args.ach, args.volume)
    if age_values is None:
        sys.exit(1)

    # Dose distribution and inactivation
    demo_dose_distribution(case_path, age_values, args.Z)

    print(f"{'='*60}")
    print("Demo complete!")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
