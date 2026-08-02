"""Segregated-flow model (SFM) for predicting UV inactivation from fluence + RTD.

Implements the framework from Blatchley et al. "Inactivation of Aerosol-Laden
Viruses Resulting From Exposure to UV-C Radiation": predicts overall reactor
inactivation by combining:

1. Spatial dose distribution (fluence rate × residence time at each cell)
2. Batch kinetics (first-order response to UV exposure)
3. Residence time or dose distribution weighting

The core segregated-flow equation is:

    (N/N₀)_reactor = ∫ (N/N₀)_batch(D) · E(D) dD

where E(D) is the dose distribution (fraction of air in dose range [D, D+dD])
and (N/N₀)_batch(D) = exp(-k·D) for first-order kinetics with rate constant k.
"""
import numpy as np

from .age_analysis import build_rtd_histogram, rtd_from_histogram


def compute_dose_at_cells(fluence_rate, age_values):
    """Compute UV dose received at each cell over its residence time.

    dose = fluence_rate [mW/cm² = mJ/(cm²·s)] × age [s]
         = mJ/cm² (milli-joules per square centimeter)

    fluence_rate: (N,) array [mW/cm²]
    age_values: (N,) array [s]

    Returns (N,) array of doses [mJ/cm²]
    """
    fluence_rate = np.asarray(fluence_rate, dtype=float)
    age_values = np.asarray(age_values, dtype=float)
    return fluence_rate * age_values  # mW/cm² × s = mJ/cm²


def build_dose_distribution(dose_values, n_bins=50):
    """Build a histogram of doses from spatial dose field.

    dose_values: (N,) array of per-cell doses [mJ/cm²]
    n_bins: number of histogram bins

    Returns (bin_edges, bin_counts) where:
    - bin_edges: (n_bins+1,) array of bin boundaries [mJ/cm²]
    - bin_counts: (n_bins,) array of cell counts per bin (unnormalized)
    """
    dose = np.asarray(dose_values, dtype=float)
    counts, edges = np.histogram(dose, bins=n_bins)
    return edges, counts


def dose_distribution_function(bin_edges, bin_counts):
    """Convert histogram bin counts into dose distribution density form E(D).

    E(D) is a probability density function such that:
    ∫ E(D) dD = 1  (normalization)

    bin_edges: (n_bins+1,) array [mJ/cm²]
    bin_counts: (n_bins,) array (unnormalized cell counts)

    Returns (bin_centers, E_D) where:
    - bin_centers: (n_bins,) midpoints [mJ/cm²]
    - E_D: (n_bins,) density values (normalized to unit integral)
    """
    bin_edges = np.asarray(bin_edges, dtype=float)
    bin_counts = np.asarray(bin_counts, dtype=float)

    bin_widths = np.diff(bin_edges)
    bin_centers = bin_edges[:-1] + bin_widths / 2

    total_count = bin_counts.sum()
    if total_count <= 0:
        raise ValueError("No dose samples in histogram")

    E_D = bin_counts / (total_count * bin_widths)
    return bin_centers, E_D


def batch_inactivation_response(dose, k):
    """Fraction of pathogen remaining after UV exposure (first-order kinetics).

    Assumes first-order kinetics: d[N]/dt = -k·[N]
    Integrated over a dose D (fluence rate E × time t): N/N₀ = exp(-k·D)

    dose: (M,) array or scalar, dose received [mJ/cm²]
    k: inactivation rate constant [cm²/mJ]

    Returns (M,) array or scalar, survival fraction N/N₀ (unitless, 0-1)
    """
    return np.exp(-np.asarray(k, dtype=float) * np.asarray(dose, dtype=float))


def segregated_flow_inactivation(dose_centers, E_D, k):
    """Predict overall reactor inactivation via segregated-flow model.

    Integrates batch kinetics weighted by the dose distribution:

        ∫ exp(-k·D) · E(D) dD ≈ Σ exp(-k·D_i) · E(D_i) · ΔD_i

    dose_centers: (n_bins,) array of dose bin midpoints [mJ/cm²]
    E_D: (n_bins,) array of dose distribution density [1/(mJ/cm²)]
    k: inactivation rate constant [cm²/mJ]

    Returns:
    - N_over_N0: overall survival fraction (unitless, 0-1)
    """
    dose_centers = np.asarray(dose_centers, dtype=float)
    E_D = np.asarray(E_D, dtype=float)
    k = float(k)

    # bin width (assumed uniform for rectangular integration)
    if len(dose_centers) > 1:
        bin_width = float(dose_centers[1] - dose_centers[0])
    else:
        bin_width = 1.0  # degenerate case: only 1 point

    batch_response = batch_inactivation_response(dose_centers, k)
    N_over_N0 = np.sum(batch_response * E_D * bin_width)

    return float(N_over_N0)


def compute_dose_distribution_from_cfd(case_dir, cell_centers, k_or_Z, room_volume=None):
    """End-to-end dose distribution and inactivation prediction.

    Combines CFD fluence + age field to compute expected inactivation for
    a given pathogen.

    case_dir: path to OpenFOAM case directory
    cell_centers: (N, 3) array of cell center coordinates [m]
    k_or_Z: inactivation rate constant (k) [1/s]
    room_volume: room volume [m³], optional, for eACH_uv computation

    Returns dict with:
    - dose_values: (N,) per-cell doses [mJ/cm²]
    - dose_statistics: mean, std, min, max dose
    - dose_distribution: {bin_centers, E_D, bin_widths}
    - inactivation: N/N₀ survival fraction
    - inactivation_log10: log₁₀(N/N₀) (negative = log reduction, or -log CFU/mL)

    Note: This function requires guv_calcs and may not work without lamp geometry.
    For simpler use cases, compute_dose_at_cells + segregated_flow_inactivation
    can be called directly with pre-computed fluence rates.
    """
    from .case_io import read_latest_time_field
    from .fluence import compute_fluence_at_points
    from .age_analysis import read_age_field

    # Get fluence rate and age from CFD
    fluence_rate = compute_fluence_at_points(case_dir, cell_centers)  # mW/cm²
    age_values = read_age_field(case_dir)  # s

    # Compute dose at each cell
    dose_values = compute_dose_at_cells(fluence_rate, age_values)

    # Dose statistics
    dose_stats = {
        "mean": float(np.mean(dose_values)),
        "std": float(np.std(dose_values)),
        "min": float(np.min(dose_values)),
        "max": float(np.max(dose_values)),
    }

    # Build dose distribution
    bin_edges, bin_counts = build_dose_distribution(dose_values, n_bins=50)
    bin_centers, E_D = dose_distribution_function(bin_edges, bin_counts)
    bin_widths = np.diff(bin_edges)

    # Ensure k is a scalar rate constant (not Z sensitivity)
    if not isinstance(k_or_Z, (int, float)):
        raise TypeError("k_or_Z must be a scalar")
    k = float(k_or_Z)

    # Predict inactivation
    N_over_N0 = segregated_flow_inactivation(bin_centers, E_D, k)
    log10_survival = np.log10(max(N_over_N0, 1e-10))  # avoid log(0)

    result = {
        "dose_values": dose_values.tolist() if isinstance(dose_values, np.ndarray) else dose_values,
        "dose_statistics": dose_stats,
        "dose_distribution": {
            "bin_centers": bin_centers.tolist(),
            "E_D": E_D.tolist(),
            "bin_widths": bin_widths.tolist(),
        },
        "inactivation": {
            "survival_fraction": N_over_N0,
            "log10_survival": float(log10_survival),
            "k_cm2_per_mJ": k,
        },
    }

    # Optional: eACH_uv if room volume is given
    if room_volume is not None:
        mean_age = float(np.mean(age_values))
        if mean_age > 0:
            mean_fluence = float(np.mean(fluence_rate))
            mean_rate_per_s = mean_fluence * 1e-3 * k  # mW/cm² × k × 1e-3
            eACH_uv = mean_rate_per_s * 3600.0  # [1/hr]
            result["eACH_uv_from_dose_distribution"] = eACH_uv

    return result
