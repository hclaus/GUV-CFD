"""Analyze the volAverage(T) decay curve from a pimpleFoam run: fit an
effective total decay rate and derive the *effective* eACH_UV implied by
the real (imperfectly mixed) CFD result - as opposed to the well-mixed
eACH_UV computed directly from volume-averaged fluence rate
(fluence.compute_well_mixed_eACH), which implicitly assumes perfect
instantaneous mixing.
"""
import json
import re
import warnings

import numpy as np
from scipy.optimize import curve_fit, OptimizeWarning


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


def check_plateau_windowed(t, T, frac=0.15, rel_tol=0.01):
    """Has a value curve genuinely plateaued (steady state reached), or did
    the run just exhaust its iteration budget while still drifting?

    Uses the SAME windowed_stats() the reported T_ss itself comes from -
    the trailing `frac` fraction of the dense, every-iteration live series
    - rather than a separate, cruder check. An earlier version compared
    just the last 5 *sparse* postProcess-cadence samples' (max-min)/mean
    "spread," which is both noisier (5 points, and a spread isn't a real
    deviation measure) and a genuinely different signal than the T_ss the
    run actually reports - it could (and did, on a real run) flag "NOT YET
    PLATEAUED" while the dense windowed CV showed a tight, clearly-settled
    0.69% - misleadingly implying the reported T_ss was less trustworthy
    than it actually was. Using the same statistic for both the log
    message and the reported value keeps them consistent by construction.
    """
    _, _, cv, _, _ = windowed_stats(t, T, frac=frac)
    converged = cv is not None and cv <= rel_tol
    return converged, cv


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


def windowed_stats_detrended(t, T, frac=0.15):
    """Same shape/window as windowed_stats() (mean, std, cv, n, window_span)
    but std/cv are computed from the RESIDUAL after removing a linear
    trend fit to the window, not the raw spread around the mean.

    A run that's still slowly converging (not yet truly flat) has a
    trailing window whose mean is itself drifting - raw std/CV conflates
    that systematic drift with genuine fluctuation/noise, and can look
    "tight" even while the average is still climbing several percent
    (confirmed on a real run: 0.64% raw CV, but detrending showed most of
    that was a 2.2%-of-mean drift over the window, not noise - residual
    CV was only 0.07%). The residual CV isolates the part that actually
    indicates instability/fluctuation, which is what's worth reporting to
    a user trying to judge "is this noisy."

    Needs at least 3 points to have any residual degrees of freedom left
    after fitting slope+intercept (a line fits exactly 2 points with zero
    residual - meaningless, not "no noise") - falls back to the plain
    mean-relative std for n<=2, same as windowed_stats().

    This does NOT affect plateau/convergence detection (see
    check_plateau_windowed, which intentionally still uses the raw,
    non-detrended CV) - only what gets reported as "how noisy is this."
    """
    T = np.asarray(T, dtype=float)
    t = np.asarray(t, dtype=float)
    n = max(2, round(len(T) * frac))
    tail_T = T[-n:]
    tail_t = t[-n:]
    mean = float(tail_T.mean())
    if n > 2:
        slope, intercept = np.polyfit(tail_t, tail_T, 1)
        residuals = tail_T - (slope * tail_t + intercept)
        std = float(residuals.std(ddof=2))
    else:
        std = float(tail_T.std(ddof=1))
    cv = (std / mean) if mean else None
    window_span = float(t[-1] - t[-n])
    return mean, std, cv, n, window_span


def fit_asymptotic_value(t, T, fit_frac=0.5):
    """Extrapolate a still-converging curve to its true n->infinity value,
    by fitting T(n) = Tinf - A*exp(-n/tau) (a single-exponential approach
    to equilibrium - the natural shape of SIMPLE's outer-iteration
    convergence, and of a genuine physical relaxation toward steady
    state) over the trailing `fit_frac` fraction of the series, rather
    than averaging a window (which is provably biased low/high whenever
    the curve hasn't fully flattened within the given iteration budget -
    confirmed on a real run: last-sample and every windowed average
    tried were all ~3% off this fit's Tinf, despite an excellent,
    near-pure-noise fit residual of 0.04% of Tinf).

    Returns a dict {Tinf, A, tau, fit_std, fit_cv} on success, or None if
    the fit fails to converge (e.g. too little data, or the curve isn't
    well-described by a single exponential - genuinely oscillating/
    unstable data, or a curve that hasn't started bending toward an
    asymptote yet) - callers should treat None as "extrapolation not
    available," not an error, and fall back to the windowed average alone.
    """
    T = np.asarray(T, dtype=float)
    t = np.asarray(t, dtype=float)
    n = len(T)
    if n < 10:
        return None
    start = int(n * (1 - fit_frac))
    tf = t[start:] - t[start]
    Tf = T[start:]

    def model(n_, Tinf, A, tau):
        return Tinf - A * np.exp(-n_ / tau)

    p0 = [Tf[-1], Tf[-1] - Tf[0], max((tf[-1] - tf[0]) / 3, 1.0)]
    try:
        with warnings.catch_warnings():
            # A flat/degenerate window (e.g. A~0, no real exponential
            # shape) legitimately can't get a meaningful covariance
            # estimate - expected and already handled below via the
            # residual-based fit_cv, not via pcov, so this is noise, not
            # a real problem to surface.
            warnings.filterwarnings("ignore", category=OptimizeWarning)
            popt, _ = curve_fit(model, tf, Tf, p0=p0, maxfev=20000)
    except (RuntimeError, ValueError):
        return None
    Tinf, A, tau = (float(v) for v in popt)
    if tau <= 0 or not np.isfinite(Tinf):
        return None
    residuals = Tf - model(tf, *popt)
    fit_std = float(residuals.std())
    fit_cv = (fit_std / Tinf) if Tinf else None
    return {"Tinf": Tinf, "A": A, "tau": tau, "fit_std": fit_std, "fit_cv": fit_cv}


def check_t_infinity_stability(history, rel_tol=0.02, streak=3):
    """Has a sequence of T-infinity extrapolation estimates (one per check
    interval during a still-running phase, oldest first, None for a check
    where fit_asymptotic_value failed) stabilized - used to stop a
    steady-state phase early once further iterations clearly wouldn't
    change the extrapolated answer, rather than always running the full
    configured iteration budget.

    True once the last `streak` estimates are ALL non-None and mutually
    within rel_tol of each other ((max-min)/mean of that group) - a
    "spread of the last N" check (matching mesh_gen/decay_analysis'
    existing plateau-style checks), not a pairwise consecutive-step
    comparison. That distinction matters: pairwise chaining has a blind
    spot for a slow, steady monotonic drift, where every individual step
    can be "small enough" while the estimate keeps moving cumulatively -
    a group spread check catches that a 3-point group hasn't actually
    settled even if each step within it looked small.

    Backtested against a real run (500-iteration check interval): 1%
    tolerance only stopped at 82% of the full budget (not much saved);
    2-3% stopped consistently at 64% - a real, non-fragile saving (the
    same stop point across a 2-3x range of tolerance values is a good
    sign the criterion isn't a hair-trigger on this data).

    Needs at least `streak` estimates to say anything (returns False
    otherwise - not enough history yet, not a genuine "unstable" verdict).
    """
    if len(history) < streak:
        return False
    tail = history[-streak:]
    if any(v is None for v in tail):
        return False
    mean = sum(tail) / streak
    if mean == 0:
        return False
    spread = (max(tail) - min(tail)) / abs(mean)
    return spread <= rel_tol


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
