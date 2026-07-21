import inspect
from types import SimpleNamespace

import guvcfd.run_pipeline as run_pipeline
from guvcfd.run_pipeline import (
    FlowConvergenceUndecided, _is_stable_oscillation, _load_history, _oscillation_diagnostic, _save_history,
    case_awaiting_flow_decision, check_ach_delivery, continue_flow_convergence, converge_flow_field,
    resume_case_setup, setup_case,
)


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


def test_converge_flow_field_returns_converged_flag():
    # Regression guard: setup_case() unpacks (latest_time, converged) - if
    # this ever goes back to a bare string return, that unpacking silently
    # breaks (or worse, silently mis-assigns) rather than erroring loudly.
    src = inspect.getsource(converge_flow_field)
    assert "return str(total_run), converged" in src


def _fake_wsl_result(stdout):
    return SimpleNamespace(stdout=stdout, returncode=0)


def test_check_ach_delivery_within_tolerance(monkeypatch, tmp_path):
    written = {}

    def fake_run_wsl_or_raise(cmd, cwd_wsl, step_name):
        written["cmd"] = cmd
        return _fake_wsl_result("sum(outlet) of phi = 0.0273193")

    monkeypatch.setattr(run_pipeline, "_run_wsl_or_raise", fake_run_wsl_or_raise)
    monkeypatch.setattr(run_pipeline, "_run_wsl", lambda cmd, cwd_wsl: _fake_wsl_result(""))

    (tmp_path / "system").mkdir()
    result = check_ach_delivery(str(tmp_path), room_volume=64.8, ach=1.5, tol=0.10, log_fn=lambda *a: None)

    # nominal = 1.5 * 64.8 / 3600 = 0.027; measured 0.0273193 -> ratio ~1.012
    assert result["within_tolerance"] is True
    assert result["nominal_flow_rate"] == 1.5 * 64.8 / 3600.0
    assert abs(result["measured_flow_rate"] - 0.0273193) < 1e-9
    assert (tmp_path / "system" / "flowRateDict").exists()


def test_check_ach_delivery_outside_tolerance_flags_it(monkeypatch, tmp_path):
    def fake_run_wsl_or_raise(cmd, cwd_wsl, step_name):
        # Reproduces the real under-delivering-diffuser case: only ~38% of
        # nominal actually leaves through the outlet.
        return _fake_wsl_result("sum(outlet) of phi = 0.0103496")

    monkeypatch.setattr(run_pipeline, "_run_wsl_or_raise", fake_run_wsl_or_raise)
    monkeypatch.setattr(run_pipeline, "_run_wsl", lambda cmd, cwd_wsl: _fake_wsl_result(""))

    (tmp_path / "system").mkdir()
    logged = []
    result = check_ach_delivery(str(tmp_path), room_volume=64.8, ach=1.5, tol=0.10, log_fn=logged.append)

    assert result["within_tolerance"] is False
    assert result["ratio"] < 0.9
    assert any("WARNING" in line for line in logged)


def test_check_ach_delivery_sums_multiple_outlet_patches(monkeypatch, tmp_path):
    def fake_run_wsl_or_raise(cmd, cwd_wsl, step_name):
        return _fake_wsl_result(
            "sum(outlet) of phi = 0.015\nsum(outlet2) of phi = 0.012"
        )

    monkeypatch.setattr(run_pipeline, "_run_wsl_or_raise", fake_run_wsl_or_raise)
    monkeypatch.setattr(run_pipeline, "_run_wsl", lambda cmd, cwd_wsl: _fake_wsl_result(""))

    (tmp_path / "system").mkdir()
    result = check_ach_delivery(str(tmp_path), room_volume=64.8, ach=1.5,
                                 outlet_patches=("outlet", "outlet2"), tol=0.10, log_fn=lambda *a: None)
    assert abs(result["measured_flow_rate"] - 0.027) < 1e-9


def test_check_ach_delivery_raises_on_unparseable_output(monkeypatch, tmp_path):
    monkeypatch.setattr(run_pipeline, "_run_wsl_or_raise",
                         lambda cmd, cwd_wsl, step_name: _fake_wsl_result("nothing useful here"))
    monkeypatch.setattr(run_pipeline, "_run_wsl", lambda cmd, cwd_wsl: _fake_wsl_result(""))

    (tmp_path / "system").mkdir()
    try:
        check_ach_delivery(str(tmp_path), room_volume=64.8, ach=1.5, log_fn=lambda *a: None)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


# --- Persisted chunk history + FlowConvergenceUndecided diagnostics ---

def test_history_round_trips_through_disk(tmp_path):
    history = [{"iteration": 500, "value": 0.01}, {"iteration": 1000, "value": 0.012}]
    _save_history(str(tmp_path), history)
    assert _load_history(str(tmp_path)) == history


def test_history_missing_file_returns_empty_list(tmp_path):
    assert _load_history(str(tmp_path)) == []


def test_history_corrupted_file_returns_empty_list_not_crash(tmp_path):
    (tmp_path / "flow_convergence_history.json").write_text("{not valid json")
    assert _load_history(str(tmp_path)) == []


def _hist(values, start_iter=500, step=500):
    return [{"iteration": start_iter + i * step, "value": v} for i, v in enumerate(values)]


def test_oscillation_diagnostic_flags_insufficient_history():
    # Exactly the real failure this fixes: 10 chunks of history, window=6
    # needs 12 - must say "not enough evidence", not claim a verdict.
    history = _hist([0.15] * 10)
    diag = _oscillation_diagnostic(history, window=6, growth_tol=1.5, rel_tol=0.02,
                                    n_iterations=500, check_field="p")
    assert diag["insufficient_history"] is True
    assert diag["bounded"] is None
    assert diag["chunks_available"] == 10
    assert diag["chunks_needed_for_oscillation_check"] == 12
    assert "Not enough chunk history" in diag["summary"]
    assert "NOT the same as having checked" in diag["summary"]


def test_oscillation_diagnostic_recognizes_bounded_oscillation_with_enough_history():
    older = [0.010, 0.030, 0.012, 0.028, 0.011, 0.031]
    newer = [0.012, 0.029, 0.010, 0.032, 0.013, 0.027]
    history = _hist(older + newer)
    diag = _oscillation_diagnostic(history, window=6, growth_tol=1.5, rel_tol=0.02,
                                    n_iterations=500, check_field="p")
    assert diag["insufficient_history"] is False
    assert diag["bounded"] is True
    assert "stable, bounded oscillation" in diag["summary"]


def test_oscillation_diagnostic_flags_genuine_growth():
    older = [0.0100, 0.0110, 0.0105, 0.0108, 0.0102, 0.0107]
    newer = [0.0050, 0.0500, 0.0010, 0.0800, 0.0005, 0.1200]
    history = _hist(older + newer)
    diag = _oscillation_diagnostic(history, window=6, growth_tol=1.5, rel_tol=0.02,
                                    n_iterations=500, check_field="p")
    assert diag["insufficient_history"] is False
    assert diag["bounded"] is False
    assert "not recommended" in diag["summary"]


def test_converge_flow_field_raises_flow_convergence_undecided_not_runtime_error(monkeypatch, tmp_path):
    # With max_iterations capped below 2*oscillation_window*n_iterations,
    # the acceptance check can never be reached - must raise
    # FlowConvergenceUndecided (with real diagnostic data attached), not a
    # bare, unstructured RuntimeError.
    call_count = {"n": 0}

    def fake_run_wsl(cmd, cwd_wsl):
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    def fake_run_wsl_or_raise(cmd, cwd_wsl, step_name):
        if "ls -d" in cmd:
            call_count["n"] += 1
            return SimpleNamespace(stdout=str(call_count["n"] * 500), returncode=0)
        if "ls " in cmd and "grep" in cmd:
            return SimpleNamespace(stdout="U p k omega nut phi", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    def fake_run_wsl_streaming(cmd, cwd_wsl, on_line=None, should_stop=None, kill_pattern=None):
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(run_pipeline, "_run_wsl", fake_run_wsl)
    monkeypatch.setattr(run_pipeline, "_run_wsl_or_raise", fake_run_wsl_or_raise)
    monkeypatch.setattr(run_pipeline, "_run_wsl_streaming", fake_run_wsl_streaming)
    # Alternates each chunk (~50% relative change every time) - large
    # enough that it never satisfies rel_tol on its own, but with only 10
    # chunks total (below the 12 the window=6 oscillation check needs),
    # there still isn't enough evidence to call it a stable oscillation.
    read_calls = {"n": 0}

    def fake_read_vol_average(path):
        read_calls["n"] += 1
        return [0], [0.10 if read_calls["n"] % 2 else 0.20]

    monkeypatch.setattr(run_pipeline, "read_vol_average_dat", fake_read_vol_average)
    # Local (non-WSL) file manipulation this test isn't exercising - only
    # the chunk-loop/verdict logic below matters here.
    monkeypatch.setattr(run_pipeline, "set_function_object_enabled", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "ensure_simple_fvsolution", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "write_fvoptions_file", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "set_control_dict_time", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "write_vol_average_dict", lambda *a, **k: None)

    try:
        converge_flow_field(str(tmp_path), n_iterations=500, max_iterations=5000,
                             oscillation_window=6, log_fn=lambda *a: None)
        assert False, "expected FlowConvergenceUndecided"
    except FlowConvergenceUndecided as e:
        assert e.total_iterations == 5000
        assert e.diagnostic["insufficient_history"] is True
        assert e.diagnostic["chunks_available"] == 10
        assert e.diagnostic["chunks_needed_for_oscillation_check"] == 12
    # And the history must have survived to disk for a later resume.
    assert len(_load_history(str(tmp_path))) == 10


def test_continue_flow_convergence_extends_max_iterations_from_persisted_history(monkeypatch, tmp_path):
    _save_history(str(tmp_path), _hist([0.15] * 10))  # last iteration = 5000

    captured = {}

    def fake_converge_flow_field(case_dir, **kwargs):
        captured.update(kwargs)
        return ("6500", True)

    monkeypatch.setattr(run_pipeline, "converge_flow_field", fake_converge_flow_field)
    result = continue_flow_convergence(str(tmp_path), additional_iterations=1500, n_iterations=500,
                                        log_fn=lambda *a: None)
    assert result == ("6500", True)
    assert captured["max_iterations"] == 5000 + 1500
    assert captured["resume"] is True


def test_resume_case_setup_rejects_unknown_decision(tmp_path):
    try:
        resume_case_setup(str(tmp_path), "unused.guv", "sideways", ach=1.5, Z=2.0)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_resume_case_setup_continue_requires_additional_iterations(monkeypatch, tmp_path):
    # Guards against a caller forgetting additional_iterations for
    # decision="continue" silently doing nothing useful.
    class _FakeRoom:
        x, y, z, units, lamps = 4.0, 6.0, 2.7, "meters", []

    class _FakeProject:
        rooms = {"a": _FakeRoom()}

        @staticmethod
        def load(path):
            return _FakeProject()

    monkeypatch.setattr(run_pipeline, "Project", _FakeProject)
    try:
        resume_case_setup(str(tmp_path), "unused.guv", "continue", ach=1.5, Z=2.0)
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- case_awaiting_flow_decision: resuming from a FRESH server session ---

def test_case_awaiting_flow_decision_none_when_no_history(tmp_path):
    assert case_awaiting_flow_decision(str(tmp_path)) is None


def test_case_awaiting_flow_decision_detects_a_paused_case(tmp_path):
    _save_history(str(tmp_path), _hist([0.15] * 10))  # 10 chunks, no verdict, no fluenceRate written
    result = case_awaiting_flow_decision(str(tmp_path), oscillation_window=6)
    assert result is not None
    assert result["total_iterations"] == 5000
    assert result["diagnostic"]["insufficient_history"] is True


def test_case_awaiting_flow_decision_none_once_resolved(tmp_path):
    # Same history, but fluenceRate exists - _finish_case_setup already ran,
    # so this case is NOT stuck, regardless of how the history itself looks.
    _save_history(str(tmp_path), _hist([0.15] * 10))
    (tmp_path / "0").mkdir()
    (tmp_path / "0" / "fluenceRate").write_text("resolved")
    assert case_awaiting_flow_decision(str(tmp_path)) is None
