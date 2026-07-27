from guvcfd import app as guvcfd_app


def test_should_pause_reflects_run_state():
    guvcfd_app._run_state["pause_requested"] = False
    assert guvcfd_app._should_pause() is False
    guvcfd_app._run_state["pause_requested"] = True
    assert guvcfd_app._should_pause() is True
    guvcfd_app._run_state["pause_requested"] = False


def test_reset_run_progress_clears_pause_requested():
    guvcfd_app._run_state["pause_requested"] = True
    guvcfd_app._reset_run_progress("decay")
    assert guvcfd_app._run_state["pause_requested"] is False


def test_toggle_pause_run_only_acts_while_running():
    guvcfd_app._run_state["status"] = "idle"
    guvcfd_app._run_state["pause_requested"] = False
    guvcfd_app._toggle_pause_run(1)
    assert guvcfd_app._run_state["pause_requested"] is False  # no-op, not running

    guvcfd_app._run_state["status"] = "running"
    guvcfd_app._toggle_pause_run(1)
    assert guvcfd_app._run_state["pause_requested"] is True

    guvcfd_app._toggle_pause_run(1)
    assert guvcfd_app._run_state["pause_requested"] is False
    guvcfd_app._run_state["status"] = "idle"


def test_scenario_should_pause_reflects_scenario_state():
    guvcfd_app._scenario_state["pause_requested"] = False
    assert guvcfd_app._scenario_should_pause() is False
    guvcfd_app._scenario_state["pause_requested"] = True
    assert guvcfd_app._scenario_should_pause() is True
    guvcfd_app._scenario_state["pause_requested"] = False


def test_toggle_pause_scenario_sweep_only_acts_while_running():
    guvcfd_app._scenario_state["status"] = "idle"
    guvcfd_app._scenario_state["pause_requested"] = False
    guvcfd_app._toggle_pause_scenario_sweep(1)
    assert guvcfd_app._scenario_state["pause_requested"] is False  # no-op, not running

    guvcfd_app._scenario_state["status"] = "running"
    guvcfd_app._toggle_pause_scenario_sweep(1)
    assert guvcfd_app._scenario_state["pause_requested"] is True

    guvcfd_app._toggle_pause_scenario_sweep(1)
    assert guvcfd_app._scenario_state["pause_requested"] is False
    guvcfd_app._scenario_state["status"] = "idle"


def test_poll_run_shows_paused_status_and_continue_label():
    guvcfd_app._run_state["status"] = "running"
    guvcfd_app._run_state["pause_requested"] = True
    guvcfd_app._run_state["log"] = []
    guvcfd_app._run_state["steps"] = []
    guvcfd_app._run_state["step_status"] = {}
    result = guvcfd_app._poll_run(1)
    status_text = result[1]
    pause_btn_label = result[7]
    assert "Paused" in status_text
    assert pause_btn_label == "Continue"
    guvcfd_app._run_state["status"] = "idle"
    guvcfd_app._run_state["pause_requested"] = False


def test_poll_scenario_shows_paused_status_and_continue_label():
    guvcfd_app._scenario_state["status"] = "running"
    guvcfd_app._scenario_state["pause_requested"] = True
    guvcfd_app._scenario_state["log"] = []
    guvcfd_app._scenario_state["combos"] = []
    guvcfd_app._scenario_state["results"] = {}
    guvcfd_app._scenario_state["live_status"] = {}
    result = guvcfd_app._poll_scenario(1)
    status_text = result[2]
    pause_btn_label = result[8]
    assert "Paused" in status_text
    assert pause_btn_label == "Continue Sweep"
    guvcfd_app._scenario_state["status"] = "idle"
    guvcfd_app._scenario_state["pause_requested"] = False
