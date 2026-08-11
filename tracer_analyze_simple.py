#!/usr/bin/env python3
"""
Simplified tracer analysis: RTD only (no fluence/dose distribution).
Works without guv_calcs dependency.

For full dose distribution analysis, use tracer_demo.py (requires lamp geometry).
"""
import sys
from pathlib import Path
import numpy as np

def get_case_dir():
    """Ask user for case directory."""
    print("\n" + "="*70)
    print("TRACER ANALYSIS - Residence Time Distribution Only")
    print("="*70 + "\n")

    while True:
        case_path = input("Enter case directory path (or 'q' to quit): ").strip()
        if case_path.lower() == 'q':
            print("Cancelled.")
            sys.exit(0)

        case = Path(case_path)
        if case.is_dir():
            return case
        print(f"ERROR: Directory not found: {case_path}")
        print("Try again.\n")

def get_params():
    """Ask user for analysis parameters."""
    print("\n" + "-"*70)
    print("CASE PARAMETERS")
    print("-"*70 + "\n")

    # ACH
    while True:
        try:
            ach = float(input("Nominal ACH [1/hr] (e.g., 6): "))
            if ach > 0:
                break
            print("ACH must be positive")
        except ValueError:
            print("Invalid number")

    return {"ach": ach}

def run_rtd_analysis(case_dir, params):
    """Extract and display RTD from age field."""
    try:
        from guvcfd.age_analysis import (
            read_age_field, compute_age_statistics, rtd_from_age_field,
            ashrae_air_change_effectiveness
        )
    except ImportError as e:
        print(f"\nERROR: Could not import age_analysis module: {e}")
        return False

    print("\n" + "="*70)
    print("RUNNING RTD ANALYSIS...")
    print("="*70 + "\n")

    try:
        # Read age field
        print("Reading age field...")
        age_values = read_age_field(case_dir)
        print(f"  Success: {len(age_values)} cells\n")

    except Exception as e:
        print(f"ERROR: Could not read age field")
        print(f"  {e}")
        print("\n  Has the case been run with the new age-of-air solver?")
        return False

    # Age statistics
    stats = compute_age_statistics(age_values)
    print("Age Field Statistics:")
    print(f"  Mean age:  {stats['mean_age']:.2f} s")
    print(f"  Std dev:   {stats['std_age']:.2f} s")
    print(f"  Min / Max: {stats['min_age']:.2f} s / {stats['max_age']:.2f} s\n")

    # RTD extraction
    rtd = rtd_from_age_field(age_values, n_bins=50)
    integral = np.trapezoid(rtd['E_t'], rtd['bin_centers'])

    print("Residence Time Distribution (RTD):")
    print(f"  Bins:      {len(rtd['bin_centers'])}")
    print(f"  Peak E(t): {rtd['E_t'].max():.4e} s^-1")
    print(f"  Integral:  {integral:.4f} (should ~ 1.0)\n")

    # ASHRAE effectiveness
    mixing = ashrae_air_change_effectiveness(age_values, params['ach'])
    print("ASHRAE Air-Change Effectiveness:")
    print(f"  Target ACH:      {params['ach']:.1f} 1/hr")
    print(f"  Effective ACH:   {mixing['effective_ach']:.2f} 1/hr")
    print(f"  Effectiveness:   {100*mixing['effectiveness']:.1f}%")

    if mixing['effectiveness'] < 0.8:
        print(f"  -> Poor mixing (significant stagnant zones)")
    elif mixing['effectiveness'] < 1.0:
        print(f"  -> Reasonable mixing (some non-uniformity)")
    else:
        print(f"  -> Good mixing (close to ideal)")

    print("\n" + "="*70)
    print("RTD ANALYSIS COMPLETE")
    print("="*70 + "\n")

    print("For full dose-distribution analysis (table + plots), run:")
    print("  python tracer_dose_report.py <case>")

    return True

def main():
    try:
        case_dir = get_case_dir()
        params = get_params()

        success = run_rtd_analysis(case_dir, params)

        if success:
            input("\nPress Enter to exit...")
        else:
            print("\nAnalysis failed. Check the error messages above.")
            input("Press Enter to exit...")
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n\nCancelled by user.")
        sys.exit(0)

if __name__ == "__main__":
    main()
