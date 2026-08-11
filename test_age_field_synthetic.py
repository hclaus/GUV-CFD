#!/usr/bin/env python3
"""
Synthetic test: validate age-field RTD and dose-distribution modules
without requiring a full OpenFOAM run.

Creates synthetic data mimicking what a real CFD case would produce,
then tests the tracer extraction pipeline.
"""
import numpy as np
from pathlib import Path
import json

from guvcfd.age_analysis import (
    compute_age_statistics, rtd_from_age_field,
    ashrae_air_change_effectiveness
)
from guvcfd.dose_distribution import (
    compute_dose_at_cells, build_dose_distribution,
    dose_distribution_function, segregated_flow_inactivation
)


def test_rtd_extraction():
    """Test RTD extraction from synthetic age field."""
    print(f"\n{'='*70}")
    print("TEST 1: RTD Extraction from Age Field")
    print(f"{'='*70}\n")

    # Create synthetic age distribution:
    # A room with mean residence time ~30 s, with some cells having shorter
    # times (near inlet) and some longer (recirculation zones).
    np.random.seed(42)
    n_cells = 1000
    age_values = np.abs(np.random.normal(30, 10, n_cells))  # mean 30s, std 10s
    age_values = np.clip(age_values, 0.1, 100)  # realistic bounds

    print(f"Synthetic age field: {n_cells} cells")
    stats = compute_age_statistics(age_values)
    print(f"  Mean age:  {stats['mean_age']:.2f} s")
    print(f"  Std:       {stats['std_age']:.2f} s")
    print(f"  Range:     {stats['min_age']:.2f} - {stats['max_age']:.2f} s\n")

    # Extract RTD
    rtd = rtd_from_age_field(age_values, n_bins=50)
    print(f"RTD (residence time distribution):")
    print(f"  Bins:      {len(rtd['bin_centers'])}")
    print(f"  Peak E(t): {rtd['E_t'].max():.4e} s^-1")

    # Check normalization
    integral = np.trapezoid(rtd['E_t'], rtd['bin_centers'])
    print(f"  Integral E(t)dt: {integral:.4f} (should ~ 1.0)")

    if abs(integral - 1.0) > 0.1:
        print("  WARNING: RTD not properly normalized!")
        return False

    print("  [OK] RTD properly normalized\n")
    return True


def test_ashrae_effectiveness():
    """Test ASHRAE air-change effectiveness calculation."""
    print(f"{'='*70}")
    print("TEST 2: ASHRAE Air-Change Effectiveness")
    print(f"{'='*70}\n")

    # Well-mixed room scenario: mean age = 3600 / target_ACH, independent of
    # room volume (see ashrae_air_change_effectiveness's docstring).
    target_ach = 6.0    # 1/hr
    mean_age_well_mixed = 3600.0 / target_ach  # seconds

    age_perfect = np.full(1000, mean_age_well_mixed)
    mixing = ashrae_air_change_effectiveness(age_perfect, target_ach)

    print(f"Perfect mixing scenario:")
    print(f"  Target ACH: {target_ach} 1/hr")
    print(f"  Mean age:   {mixing['mean_age']:.1f} s")
    print(f"  Effective ACH: {mixing['effective_ach']:.2f} 1/hr")
    print(f"  Effectiveness (ε_a): {mixing['effectiveness']:.2%}")

    if abs(mixing['effectiveness'] - 1.0) > 0.01:
        print("  [WARNING]  ERROR: Perfect mixing should have ε_a = 1.0!")
        return False
    print("  [OK] Perfect mixing gives ε_a = 1.0\n")

    # Poor mixing scenario: longer mean age = slower effective ACH
    age_poor = np.full(1000, mean_age_well_mixed * 2)  # 2x mean age
    mixing_poor = ashrae_air_change_effectiveness(age_poor, target_ach)

    print(f"Poor mixing scenario (2× mean age):")
    print(f"  Effective ACH: {mixing_poor['effective_ach']:.2f} 1/hr")
    print(f"  Effectiveness ε_a: {mixing_poor['effectiveness']:.2%}")
    print(f"  -> Only {100*mixing_poor['effectiveness']:.1f}% of nominal ACH achieved\n")

    if mixing_poor['effectiveness'] >= mixing['effectiveness']:
        print("  [WARNING]  ERROR: Longer age should give lower effectiveness!")
        return False
    print("  [OK] Longer age correctly reduces effectiveness\n")
    return True


def test_dose_distribution_and_inactivation():
    """Test dose distribution and segregated-flow inactivation prediction."""
    print(f"{'='*70}")
    print("TEST 3: Dose Distribution & Segregated-Flow Inactivation")
    print(f"{'='*70}\n")

    np.random.seed(42)
    n_cells = 1000

    # Synthetic fluence rate: mostly 10 mW/cm², some cells 5 (shadows), some 15 (hotspots)
    fluence_rate = np.abs(np.random.normal(10, 3, n_cells))
    fluence_rate = np.clip(fluence_rate, 0.1, 20)

    # Synthetic age: mean 30 s
    age_values = np.abs(np.random.normal(30, 10, n_cells))
    age_values = np.clip(age_values, 0.1, 100)

    print(f"Synthetic domain: {n_cells} cells")
    print(f"  Fluence: {fluence_rate.mean():.1f} +/- {fluence_rate.std():.1f} mW/cm²")
    print(f"  Age:     {age_values.mean():.1f} +/- {age_values.std():.1f} s\n")

    # Compute dose
    dose_values = compute_dose_at_cells(fluence_rate, age_values)
    print(f"Dose = fluence × age:")
    print(f"  Mean dose: {dose_values.mean():.4f} mJ/cm²")
    print(f"  Std:       {dose_values.std():.4f} mJ/cm²")
    print(f"  Range:     {dose_values.min():.4f} - {dose_values.max():.4f} mJ/cm²\n")

    # Build dose distribution
    bin_edges, bin_counts = build_dose_distribution(dose_values, n_bins=50)
    bin_centers, E_D = dose_distribution_function(bin_edges, bin_counts)

    # Check normalization
    integral = np.trapezoid(E_D, bin_centers)
    print(f"Dose distribution E(D):")
    print(f"  Bins: {len(bin_centers)}")
    print(f"  ∫E(D)dD: {integral:.4f} (should ~ 1.0)")

    if abs(integral - 1.0) > 0.1:
        print("  [WARNING]  WARNING: Dose distribution not properly normalized!")
        return False
    print("  [OK] Dose distribution properly normalized\n")

    # Segregated-flow inactivation prediction with Z=6
    Z = 6.0
    mean_fluence_uW_cm2 = fluence_rate.mean()
    k = Z * mean_fluence_uW_cm2 * 1e-3  # k [1/s]

    print(f"Segregated-flow model prediction (Z={Z} cm²/mJ):")
    print(f"  Mean fluence: {mean_fluence_uW_cm2:.2f} uW/cm²")
    print(f"  Computed k: {k:.4f} 1/s\n")

    N_over_N0 = segregated_flow_inactivation(bin_centers, E_D, k)
    log_reduction = -np.log10(max(N_over_N0, 1e-10))

    print(f"  N/N₀ (survival): {N_over_N0:.4e}")
    print(f"  Log reduction: {log_reduction:.2f}")
    print(f"  -> {100*N_over_N0:.2%} survive, {100*(1-N_over_N0):.1f}% inactivated\n")

    # Compare vs. well-mixed approximation
    mean_dose = dose_values.mean()
    N_over_N0_mixed = np.exp(-k * mean_dose)
    log_reduction_mixed = -np.log10(max(N_over_N0_mixed, 1e-10))

    print(f"Well-mixed approximation (average dose):")
    print(f"  Mean dose: {mean_dose:.4f} mJ/cm²")
    print(f"  N/N₀: {N_over_N0_mixed:.4e}")
    print(f"  Log reduction: {log_reduction_mixed:.2f}\n")

    if N_over_N0_mixed > 0:
        ratio = N_over_N0 / N_over_N0_mixed
        print(f"Divergence (dose-dist / well-mixed):")
        print(f"  Ratio N/N₀: {ratio:.2f}x")
        if ratio > 1.2 or ratio < 0.8:
            print(f"  -> Dose distribution MEANINGFULLY changes prediction\n")
        else:
            print(f"  -> Distributions reasonably close\n")

    return True


def main():
    print(f"\n{'='*70}")
    print("TRACER MODULE VALIDATION TESTS")
    print("Synthetic data (no OpenFOAM case required)")
    print(f"{'='*70}")

    tests = [
        ("RTD Extraction", test_rtd_extraction),
        ("ASHRAE Effectiveness", test_ashrae_effectiveness),
        ("Dose Distribution & Inactivation", test_dose_distribution_and_inactivation),
    ]

    results = {}
    for name, test_func in tests:
        try:
            results[name] = test_func()
        except Exception as e:
            print(f"\n[ERROR] TEST FAILED: {name}")
            print(f"   Error: {e}\n")
            results[name] = False

    print(f"{'='*70}")
    print("TEST SUMMARY")
    print(f"{'='*70}\n")

    for name, passed in results.items():
        status = "[OK] PASS" if passed else "[FAIL] FAIL"
        print(f"  {status}: {name}")

    all_passed = all(results.values())
    print(f"\n{'='*70}")
    if all_passed:
        print("[OK] ALL TESTS PASSED")
    else:
        print("[FAIL] SOME TESTS FAILED")
    print(f"{'='*70}\n")

    return 0 if all_passed else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
