import inspect

from guvcfd.run_pipeline import _is_stable_oscillation, converge_flow_field, setup_case


def test_flow_convergence_default_tolerance_is_one_percent():
    # Regression guard for a deliberate tuning choice: real room-
    # ventilation flows often oscillate in the 0.5-1% band without ever
    # settling further (see converge_flow_field's own docstring and the
    # bounded-oscillation acceptance fallback) - chasing 0.5% wastes
    # wall-clock time without buying real accuracy downstream.
    default = inspect.signature(converge_flow_field).parameters["rel_tol"].default
    assert default == 0.01


def test_setup_case_has_flow_rel_tol_passthrough_matching_converge_flow_field():
    # setup_case() must expose the same rel_tol converge_flow_field() itself
    # defaults to, so a caller that doesn't pass flow_rel_tol explicitly
    # (any caller predating the Settings menu, or tests) gets identical
    # behavior to before this parameter was added.
    params = inspect.signature(setup_case).parameters
    assert "flow_rel_tol" in params
    assert params["flow_rel_tol"].default == inspect.signature(converge_flow_field).parameters["rel_tol"].default


def test_not_enough_history_rejected():
    # Only 4 chunks, window=3 needs 2*3=6 - can't tell converging from
    # diverging yet, so the safe default is to say "not stable" (caller
    # keeps the hard failure).
    assert not _is_stable_oscillation([1, 2, 1, 2], window=3, growth_tol=1.5)


def test_flat_history_is_stable():
    assert _is_stable_oscillation([5.0] * 12, window=6, growth_tol=1.5)


def test_bounded_oscillation_is_accepted():
    # Mirrors the real fan-jet case: volAverage(p) swings by a large relative
    # amount chunk-to-chunk, but the swing itself isn't growing over time.
    older = [0.010, 0.030, 0.012, 0.028, 0.011, 0.031]
    newer = [0.012, 0.029, 0.010, 0.032, 0.013, 0.027]
    assert _is_stable_oscillation(older + newer, window=6, growth_tol=1.5)


def test_growing_amplitude_is_rejected():
    # Still trending/diverging: the recent window's swing is much larger
    # than the swing before it.
    older = [0.0100, 0.0110, 0.0105, 0.0108, 0.0102, 0.0107]
    newer = [0.0050, 0.0500, 0.0010, 0.0800, 0.0005, 0.1200]
    assert not _is_stable_oscillation(older + newer, window=6, growth_tol=1.5)


def test_drifting_mean_is_rejected():
    # Same bounded amplitude in both windows, but the whole thing has shifted
    # to a different level - a slow drift, not a settled oscillation.
    older = [0.008, 0.012, 0.009, 0.011, 0.0085, 0.0115]
    newer = [0.098, 0.102, 0.099, 0.101, 0.0985, 0.1015]
    assert not _is_stable_oscillation(older + newer, window=6, growth_tol=1.5)


def test_growth_at_exact_tolerance_boundary_is_accepted():
    older = [0.0, 0.10]  # amplitude 0.10
    newer = [0.0, 0.15]  # amplitude 0.15 == growth_tol(1.5) * 0.10
    assert _is_stable_oscillation(older + newer, window=2, growth_tol=1.5)


def test_growth_just_over_tolerance_is_rejected():
    older = [0.0, 0.10]        # amplitude 0.10
    newer = [0.0, 0.1501]      # amplitude just over 1.5x
    assert not _is_stable_oscillation(older + newer, window=2, growth_tol=1.5)
