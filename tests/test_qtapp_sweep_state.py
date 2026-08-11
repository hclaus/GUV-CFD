import time

from guvcfd.qtapp import sweep_state
from guvcfd.wsl_utils import StoppedByUser


def _wait_until_done(state, timeout=5.0):
    deadline = time.time() + timeout
    while state.status == "running" and time.time() < deadline:
        time.sleep(0.01)
    assert state.status != "running", "launch_extend's worker never finished"


def test_launch_extend_records_done_and_error_per_combo(monkeypatch):
    def fake_continue_decay(case_dir, end_time, write_interval, log_fn=None, should_stop=None,
                             should_pause=None):
        if "boom" in case_dir:
            raise RuntimeError("boom happened")

    monkeypatch.setattr(sweep_state.scenario_runs, "continue_decay", fake_continue_decay)

    state = sweep_state.SweepState()
    case_dirs = [("/proj/Z6_ACH3", 6.0, 3.0), ("/proj/boom_ACH3", 2.0, 3.0)]
    sweep_state.launch_extend(state, case_dirs, 900, 5)
    _wait_until_done(state)

    assert state.results[(6.0, 3.0)] == {"status": "done", "detail": None}
    assert state.results[(2.0, 3.0)]["status"] == "error"
    assert state.status == "done"
    assert state.combos == [(6.0, 3.0), (2.0, 3.0)]


def test_launch_extend_stops_on_stopped_by_user(monkeypatch):
    calls = []

    def fake_continue_decay(case_dir, end_time, write_interval, log_fn=None, should_stop=None,
                             should_pause=None):
        calls.append(case_dir)
        raise StoppedByUser("stopped mid-solve")

    monkeypatch.setattr(sweep_state.scenario_runs, "continue_decay", fake_continue_decay)

    state = sweep_state.SweepState()
    case_dirs = [("/proj/Z1_ACH3", 1.0, 3.0), ("/proj/Z2_ACH3", 2.0, 3.0)]
    sweep_state.launch_extend(state, case_dirs, 900, 5)
    _wait_until_done(state)

    # the loop breaks on StoppedByUser - the second combo is never attempted
    assert calls == ["/proj/Z1_ACH3"]
    assert (1.0, 3.0) not in state.results  # StoppedByUser never records a result for that combo


def test_launch_extend_honors_stop_requested_between_combos(monkeypatch):
    calls = []

    def fake_continue_decay(case_dir, end_time, write_interval, log_fn=None, should_stop=None,
                             should_pause=None):
        calls.append(case_dir)
        state.stop_requested = True  # simulate a Stop click after the first combo finishes

    monkeypatch.setattr(sweep_state.scenario_runs, "continue_decay", fake_continue_decay)

    state = sweep_state.SweepState()
    case_dirs = [("/proj/Z1_ACH3", 1.0, 3.0), ("/proj/Z2_ACH3", 2.0, 3.0)]
    sweep_state.launch_extend(state, case_dirs, 900, 5)
    _wait_until_done(state)

    assert calls == ["/proj/Z1_ACH3"]
    assert state.status == "stopped"
