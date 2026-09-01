"""The flow-convergence acceptance rule, replayed against the two real chunk
histories that exposed the one-sample bug (patient ward v9 and V10).

Both were declared "converged" off a single lucky chunk - v9 on 1 of 15,
V10 on 1 of 7 - each landing on an extreme of its own series rather than a
settled state, because |x_n - x_(n-1)| is smallest exactly at a turning
point of an oscillation.
"""
import pytest

from guvcfd.run_pipeline import _is_stable_oscillation, _oscillation_diagnostic

# Real flow_convergence_history.json values, 500-iteration chunks.
V10 = [0.0155186, 0.0127752, 0.0207115, 0.0158107, 0.0164664, 0.0186372, 0.0223437, 0.0223738]
V9 = [0.019887, 0.0234816, 0.0190345, 0.0192873, 0.0228815, 0.0171836, 0.0195417, 0.0240796,
      0.0208312, 0.0203138, 0.0240453, 0.0236218, 0.020664, 0.0268301, 0.0174469, 0.0172849]


def _streak(values, rel_tol):
    """Longest run of within-tolerance chunks ending at the last value -
    mirrors the in-loop counter in converge_flow_field."""
    n = 0
    for i in range(len(values) - 1, 0, -1):
        if abs(values[i] - values[i - 1]) / abs(values[i - 1]) > rel_tol:
            break
        n += 1
    return n


@pytest.mark.parametrize("name,values", [("V10", V10), ("v9", V9)])
def test_real_histories_reach_a_streak_of_only_one(name, values):
    """Each of these DID satisfy the old one-sample rule, and must not
    satisfy a 3-consecutive-chunk rule - that gap is the entire bug."""
    assert _streak(values, 0.01) == 1, name


@pytest.mark.parametrize("name,values", [("V10", V10), ("v9", V9)])
def test_the_accepted_value_was_an_extreme_not_a_settled_state(name, values):
    """Why one sample is not merely weak evidence but biased evidence: the
    test fires at turning points, so the frozen field is an extreme of the
    cycle. Both real runs stopped within one rank of an end of their own
    sorted series."""
    rank = sorted(values).index(values[-1])
    assert rank <= 1 or rank >= len(values) - 2, f"{name}: rank {rank} of {len(values)}"


def test_a_genuinely_settling_flow_still_converges():
    """The fix must not make a real plateau unconvergeable."""
    settling = [0.05, 0.03, 0.021, 0.0203, 0.02021, 0.020195, 0.0201]
    assert _streak(settling, 0.01) >= 3


def test_diagnostic_reports_the_streak_not_just_the_last_change():
    diag = _oscillation_diagnostic(
        [{"iteration": (i + 1) * 500, "value": v} for i, v in enumerate(V10)],
        window=6, growth_tol=1.5, rel_tol=0.01, n_iterations=500, check_field="p",
        converged_chunks=3)
    assert diag["converged_streak"] == 1
    assert diag["converged_chunks_required"] == 3
    # The misleading number on its own: last change looks like convergence.
    assert diag["last_chunk_rel_change"] < 0.01


def test_v9_reads_as_a_bounded_oscillation_once_there_is_enough_history():
    """v9's 16 chunks are stationary around ~0.021 - the flow is not broken,
    it is turbulent. The in-loop oscillation check should accept it, which is
    what stops the streak rule from burning the full iteration budget."""
    assert _is_stable_oscillation(V9, window=6, growth_tol=1.5) is True


def test_v10_has_too_little_history_to_claim_anything():
    """8 chunks < 2*window, so no verdict is available either way - and the
    old rule nonetheless reported 'converged'."""
    assert _is_stable_oscillation(V10, window=6, growth_tol=1.5) is False
