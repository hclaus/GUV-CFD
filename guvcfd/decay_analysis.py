"""Analyze the volAverage(T) decay curve from a pimpleFoam run: fit an
effective total decay rate and derive the *effective* eACH_UV implied by
the real (imperfectly mixed) CFD result - as opposed to the well-mixed
eACH_UV computed directly from volume-averaged fluence rate
(fluence.compute_well_mixed_eACH), which implicitly assumes perfect
instantaneous mixing.
"""
import json
import re
import numpy as np


def read_vol_average_dat(path):
    """Parse postProcessing/volAverage1/<time>/volFieldValue.dat.

    Returns (t, values) arrays, skipping the '# ...' header lines.
    """
    t, values = [], []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = re.split(r"\s+", line)
            t.append(float(parts[0]))
            values.append(float(parts[1]))
    return np.array(t), np.array(values)


def fit_effective_decay_rate(t, T):
    """Least-squares fit of ln(T) = -lambda*t + c. Returns lambda [1/s].

    A real CFD decay curve (imperfect mixing) isn't a perfect single
    exponential, so this is a best-fit summary, not an exact value -
    intercept should come out close to ln(T[0]) if the fit is well-behaved.
    """
    t = np.asarray(t, dtype=float)
    T = np.asarray(T, dtype=float)
    A = np.vstack([t, np.ones_like(t)]).T
    slope, intercept = np.linalg.lstsq(A, np.log(T), rcond=None)[0]
    return -slope, intercept


def compute_effective_eACH(t, T, ventilation_ach, ventilation_lambda_per_s=None):
    """Effective eACH_UV [1/hr] implied by an actual CFD decay curve, i.e.
    what UV-only air-change-equivalent would explain the observed *total*
    decay rate once ventilation's own contribution is subtracted out.

    Compare against fluence.compute_well_mixed_eACH() (mean/max over cells,
    computed directly from fluence rate): the well-mixed number assumes
    perfect instantaneous mixing, so it's an upper bound. The gap between
    the two quantifies how much imperfect real-world mixing reduces UV's
    effective disinfection benefit versus that ideal.

    ventilation_lambda_per_s: if given, subtract this *measured* ventilation-
    only decay rate (from a UV-off control run - see
    ventilation_control.run_ventilation_only_control) instead of assuming
    ventilation_ach/3600 exactly. Real ventilation doesn't always achieve its
    nominal ACH either (the same imperfect-mixing effect this function
    already isolates for UV) - using the measured baseline removes that
    small bias from eACH_uv_effective. Defaults to the nominal ACH when not
    given (the original, uncorrected behavior).
    """
    lambda_total_effective, intercept = fit_effective_decay_rate(t, T)
    lambda_vent = ventilation_lambda_per_s if ventilation_lambda_per_s is not None else ventilation_ach / 3600.0
    eACH_uv_effective = (lambda_total_effective - lambda_vent) * 3600.0
    return eACH_uv_effective, lambda_total_effective, intercept


def check_plateau(T, window=5, rel_tol=0.01):
    """Has a value curve genuinely plateaued (steady state reached), or did
    the run just exhaust its iteration budget while still drifting?

    Compares the spread of the last `window` values against their mean; if
    that relative spread is above rel_tol, the run needs more iterations.
    Used to verify each steady-state phase actually converged rather than
    just assuming a fixed iteration budget was enough.
    """
    T = np.asarray(T, dtype=float)
    tail = T[-window:]
    spread = tail.max() - tail.min()
    mean = tail.mean()
    rel_spread = spread / mean if mean else float("inf")
    return bool(rel_spread <= rel_tol), float(rel_spread)


def windowed_stats(t, T, frac=0.15):
    """Mean/std/CV of the trailing `frac` fraction of `T` (by sample count,
    floored at 2 points so stdev is always defined) - a steadier read of
    "steady state" than T[-1] alone, especially for turbulent monitoring
    points where the instantaneous last sample can be off by 25-50%+ from
    a real converged run (see the live-volAverage validation). Since T is a
    passive linear scalar here, this window's *relative* noise (CV) doesn't
    change with source strength - only averaging over more samples (a
    live per-iteration series, not just write_interval snapshots) narrows
    the standard error of the mean.

    Returns (mean, std, cv, n, window_span) - window_span is
    t[-1] - t[-n], the window's actual duration/iteration-count, for
    labeling ("last {window_span} iterations"). cv is None when mean is 0.
    """
    T = np.asarray(T, dtype=float)
    t = np.asarray(t, dtype=float)
    n = max(2, round(len(T) * frac))
    tail = T[-n:]
    mean = float(tail.mean())
    std = float(tail.std(ddof=1))
    cv = (std / mean) if mean else None
    window_span = float(t[-1] - t[-n])
    return mean, std, cv, n, window_span


def write_results_summary(case_dir, out_path, ventilation_ach, well_mixed_eACH_mean,
                           vol_average_dat="postProcessing/volAverage1/0/volFieldValue.dat",
                           extra=None, measured_ventilation_ach=None):
    """Write a single results.json combining the well-mixed eACH (from
    setup_case's fluence computation) with the CFD-fit effective eACH (from
    an actual completed pimpleFoam run's decay curve) - everything a results
    display needs, in one file, independent of how/where it gets rendered.

    measured_ventilation_ach: the *actual* ventilation-only air-change rate
    from a UV-off control run (ventilation_control.py), if one was run
    alongside this case. When given, also writes corrected
    eACH_uv_effective_corrected/mixing_efficiency_corrected fields that
    subtract this measured baseline instead of the nominal ventilation_ach -
    see compute_effective_eACH's docstring for why that's more accurate.
    """
    dat_path = f"{case_dir}/{vol_average_dat}"
    t, T = read_vol_average_dat(dat_path)
    eACH_eff, lambda_eff, intercept = compute_effective_eACH(t, T, ventilation_ach)

    summary = {
        "ventilation_ach": ventilation_ach,
        "eACH_uv_well_mixed": well_mixed_eACH_mean,
        "eACH_uv_effective": eACH_eff,
        "mixing_efficiency": eACH_eff / well_mixed_eACH_mean if well_mixed_eACH_mean else None,
        "total_ach_well_mixed": ventilation_ach + well_mixed_eACH_mean,
        "total_ach_effective": ventilation_ach + eACH_eff,
        "lambda_total_effective_per_s": lambda_eff,
        "fit_intercept": intercept,
        "decay_curve": {"t_seconds": t.tolist(), "volAverage_T": T.tolist()},
    }

    if measured_ventilation_ach is not None:
        eACH_eff_corrected, _, _ = compute_effective_eACH(
            t, T, ventilation_ach, ventilation_lambda_per_s=measured_ventilation_ach / 3600.0)
        summary["ventilation_ach_measured"] = measured_ventilation_ach
        summary["eACH_uv_effective_corrected"] = eACH_eff_corrected
        summary["mixing_efficiency_corrected"] = (
            eACH_eff_corrected / well_mixed_eACH_mean if well_mixed_eACH_mean else None)

    if extra:
        summary.update(extra)

    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2)
    return summary
