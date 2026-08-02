"""Extract residence time distribution (RTD) and mixing metrics from age-of-air field.

The age field (solved via scalarTransport2 in controlDict) gives the mean time
since entering at each point. This module converts that spatial distribution into
an RTD (residence time distribution) E(t), which quantifies the fraction of air
in the room with residence times in a given range - the core quantity needed
for segregated-flow-model inactivation predictions (see Blatchley et al.).

Age field = Eulerian age of air (mean residence time at a fixed point).
RTD E(t) = Lagrangian residence time distribution (time spent by fluid elements
in the room before exiting).

Under the "age = steady-state time-since-inlet" model, E(t) is derived from the
spatial age distribution by converting point-wise age values into a probability
density weighted by the exit mass flux at each location.
"""
import numpy as np
from scipy import stats

from .case_io import read_latest_time_field, read_openfoam_scalar_field


def read_age_field(case_dir):
    """Read the age field from the latest time directory.

    Returns an (N,) array of per-cell age values [seconds].
    """
    return read_latest_time_field(case_dir, field_name="age")


def compute_age_statistics(age_values):
    """Compute basic summary statistics from the age field.

    age_values: (N,) array of per-cell ages [s]

    Returns dict with:
    - mean_age: volume-averaged (unweighted) mean age [s]
    - std_age: standard deviation of age across cells [s]
    - min_age, max_age: range [s]
    """
    age = np.asarray(age_values, dtype=float)
    return {
        "mean_age": float(age.mean()),
        "std_age": float(age.std()),
        "min_age": float(age.min()),
        "max_age": float(age.max()),
    }


def build_rtd_histogram(age_values, n_bins=50):
    """Build a histogram of residence times from the age field.

    This is a simplified RTD estimate: assume the age distribution directly
    represents a proxy for RTD (appropriate when the domain is at steady state,
    inlet age=0, and outlet is well-sampled). A more rigorous approach would
    require explicit exit-plane sampling or Lagrangian particle tracking.

    age_values: (N,) array of per-cell ages [s]
    n_bins: number of histogram bins

    Returns (bin_edges, bin_counts) where:
    - bin_edges: (n_bins+1,) array of bin boundaries [s]
    - bin_counts: (n_bins,) array of cell counts per bin (unnormalized)
    """
    age = np.asarray(age_values, dtype=float)
    counts, edges = np.histogram(age, bins=n_bins)
    return edges, counts


def rtd_from_histogram(bin_edges, bin_counts):
    """Convert histogram bin counts into RTD density form E(t).

    The RTD E(t) is a probability density function such that:
    ∫ E(t) dt = 1  (normalization)
    ∫ t * E(t) dt = mean residence time

    bin_edges: (n_bins+1,) array
    bin_counts: (n_bins,) array (unnormalized cell counts)

    Returns (bin_centers, E_t) where:
    - bin_centers: (n_bins,) midpoints of each bin [s]
    - E_t: (n_bins,) density values (unnormalized to unit integral)
    """
    bin_edges = np.asarray(bin_edges, dtype=float)
    bin_counts = np.asarray(bin_counts, dtype=float)

    bin_widths = np.diff(bin_edges)
    bin_centers = bin_edges[:-1] + bin_widths / 2

    total_count = bin_counts.sum()
    if total_count <= 0:
        raise ValueError("No age samples in histogram")

    # E(t) ≈ (count in bin) / (total count * bin width)
    # This ensures ∫ E(t) dt ≈ 1 across the binned range.
    E_t = bin_counts / (total_count * bin_widths)

    return bin_centers, E_t


def rtd_from_age_field(age_values, n_bins=50):
    """End-to-end RTD extraction: age field → E(t).

    Returns dict with:
    - bin_centers: (n_bins,) array of residence times [s]
    - E_t: (n_bins,) array of density values (∫E(t)dt ≈ 1)
    - statistics: dict of summary stats (mean_age, std_age, etc.)
    """
    stats_dict = compute_age_statistics(age_values)
    edges, counts = build_rtd_histogram(age_values, n_bins=n_bins)
    centers, E_t = rtd_from_histogram(edges, counts)

    return {
        "bin_centers": centers,
        "E_t": E_t,
        "statistics": stats_dict,
    }


def ashrae_air_change_effectiveness(age_values, target_ach, room_volume):
    """Compute ASHRAE 129 air-change effectiveness ε_a.

    For a given nominal ACH target, the "effective" (or "real") ACH implied by
    the actual age distribution is (room_volume / mean_age). The ratio

        ε_a = effective_ACH / target_ACH

    quantifies how well the ventilation system achieves mixing relative to an
    ideal perfectly-mixed room. A well-mixed room has ε_a ≈ 1. Stagnant
    pockets and short-circuiting reduce ε_a well below 1.

    age_values: (N,) array of per-cell ages [s]
    target_ach: nominal ventilation air-change rate [1/hr]
    room_volume: room volume [m^3]

    Returns dict with:
    - effective_ach: actual ACH implied by age distribution [1/hr]
    - effectiveness: ε_a = effective_ACH / target_ACH (dimensionless)
    """
    age = np.asarray(age_values, dtype=float)
    mean_age = age.mean()

    if mean_age <= 0:
        raise ValueError("Mean age must be positive")

    # Mean age [s] = room_volume [m^3] / exit_flow_rate [m^3/s]
    # For a room with target ACH [1/hr]:
    #   ACH = (exit_flow_rate [m^3/s] * 3600 [s/hr]) / room_volume [m^3]
    #   exit_flow_rate = ACH * room_volume / 3600
    # So:
    #   effective_ACH = (room_volume / mean_age) * 3600 [s/hr]

    effective_ach = (room_volume / mean_age) * 3600.0
    effectiveness = effective_ach / target_ach if target_ach > 0 else None

    return {
        "effective_ach": effective_ach,
        "effectiveness": effectiveness,
        "mean_age": mean_age,
    }


def write_rtd_summary(case_dir, out_path, target_ach=None, room_volume=None, n_bins=50):
    """Extract age field and write RTD analysis to a JSON-compatible dict.

    case_dir: path to OpenFOAM case
    out_path: path to write summary (currently not used - returns dict only)
    target_ach: nominal ventilation ACH [1/hr], if known (for effectiveness calc)
    room_volume: room volume [m^3], if known (for effectiveness calc)
    n_bins: number of RTD histogram bins

    Returns dict with RTD and mixing metrics, suitable for JSON serialization
    or merging into an existing results.json.
    """
    age_values = read_age_field(case_dir)
    rtd_result = rtd_from_age_field(age_values, n_bins=n_bins)

    summary = {
        "age_statistics": rtd_result["statistics"],
        "rtd": {
            "bin_centers": rtd_result["bin_centers"].tolist(),
            "E_t": rtd_result["E_t"].tolist(),
        },
    }

    if target_ach is not None and room_volume is not None:
        mixing = ashrae_air_change_effectiveness(age_values, target_ach, room_volume)
        summary["ashrae_effectiveness"] = mixing

    return summary
