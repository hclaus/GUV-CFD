from guvcfd import app as guvcfd_app


def test_continue_decay_delegates_to_scenario_runs_with_its_own_callbacks(monkeypatch):
    # _continue_decay is now a thin wrapper over scenario_runs.continue_decay
    # (moved there 2026-08-10 so the Qt app can reuse the identical logic) -
    # this just confirms the delegation wires this app's own log_fn/
    # should_stop/should_pause/solver_log_fn through correctly, and that
    # _complete_all_steps() still runs afterward. The actual behavior (post-
    # hoc volAverage path, etc.) is covered directly against
    # scenario_runs.continue_decay in tests/test_scenario_runs.py.
    captured = {}

    def fake_continue_decay(case_dir, end_time, write_interval, log_fn=None, should_stop=None,
                             should_pause=None, solver_log_fn=None):
        captured.update(case_dir=case_dir, end_time=end_time, write_interval=write_interval,
                         log_fn=log_fn, should_stop=should_stop, should_pause=should_pause,
                         solver_log_fn=solver_log_fn)
        return {"eACH_uv_effective": 1.0, "eACH_uv_well_mixed": 5.0}

    monkeypatch.setattr(guvcfd_app.scenario_runs, "continue_decay", fake_continue_decay)
    completed = []
    monkeypatch.setattr(guvcfd_app, "_complete_all_steps", lambda: completed.append(1))

    results = guvcfd_app._continue_decay("/case/dir", end_time=600, write_interval=10)

    assert captured["case_dir"] == "/case/dir"
    assert captured["end_time"] == 600 and captured["write_interval"] == 10
    assert captured["log_fn"] is guvcfd_app._run_log
    assert captured["should_stop"] is guvcfd_app._should_stop
    assert captured["should_pause"] is guvcfd_app._should_pause
    assert captured["solver_log_fn"] is guvcfd_app._track_solver_time
    assert completed == [1]
    assert results == {"eACH_uv_effective": 1.0, "eACH_uv_well_mixed": 5.0}
