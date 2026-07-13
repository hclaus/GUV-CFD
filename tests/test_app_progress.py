from guvcfd import app as guvcfd_app


def _reset():
    guvcfd_app._reset_run_progress("decay")


def test_flow_convergence_progress_is_cumulative_across_chunks():
    # Narration (chunk/budget announcements) goes through _run_log, as the
    # pipeline itself emits it; raw solver stdout ("Time = N") goes through
    # _track_solver_time instead - see _run_log's docstring for why the two
    # are no longer the same function.
    _reset()
    guvcfd_app._run_log("Flow-convergence budget: 5000 iterations max, in chunks of 500...")
    assert guvcfd_app._run_state["target_time"] == 5000.0

    guvcfd_app._run_log("Running simpleFoam iterations 1-500 (chunk size 500)...")
    for t in (1, 250, 500):
        guvcfd_app._track_solver_time(f"Time = {t}")
    assert float(guvcfd_app._run_state["current_time"]) == 500.0

    # Second chunk: solver's own Time resets to ~0, but cumulative progress
    # must keep climbing past the previous chunk's end (500), not fall back.
    guvcfd_app._run_log("Running simpleFoam iterations 501-1000 (chunk size 500)...")
    guvcfd_app._track_solver_time("Time = 1")
    assert float(guvcfd_app._run_state["current_time"]) == 501.0
    guvcfd_app._track_solver_time("Time = 500")
    assert float(guvcfd_app._run_state["current_time"]) == 1000.0
    # target_time must not have been perturbed by the per-chunk line.
    assert guvcfd_app._run_state["target_time"] == 5000.0

    guvcfd_app._run_log("Running simpleFoam iterations 1001-1500 (chunk size 500)...")
    guvcfd_app._track_solver_time("Time = 500")
    assert float(guvcfd_app._run_state["current_time"]) == 1500.0
    assert guvcfd_app._run_state["target_time"] == 5000.0


def test_progress_text_climbs_monotonically_across_chunks():
    _reset()
    guvcfd_app._run_log("Flow-convergence budget: 1500 iterations max, in chunks of 500...")
    pcts = []
    for start in (1, 501, 1001):
        guvcfd_app._run_log(f"Running simpleFoam iterations {start}-{start + 499} (chunk size 500)...")
        guvcfd_app._track_solver_time("Time = 500")
        text = guvcfd_app._solver_progress_text()
        pct = int(text.split("(")[1].split("%")[0])
        pcts.append(pct)
    assert pcts == sorted(pcts), f"progress should climb monotonically, got {pcts}"
    assert pcts[-1] == 100


def test_non_chunked_phase_still_tracks_time_directly():
    # pimpleFoam decay/steady-state phases aren't chunked - current_time
    # should track the raw Time value with no cumulative offset.
    _reset()
    guvcfd_app._run_log("Running pimpleFoam to 60.0s")
    guvcfd_app._track_solver_time("Time = 30")
    assert guvcfd_app._run_state["current_time"] == "30"
    assert guvcfd_app._run_state["chunk_base"] is None


def test_track_solver_time_does_not_append_to_visible_log():
    # The whole point of the split: raw per-iteration solver stdout must
    # not flood the narration log that step transitions/convergence
    # summaries/errors rely on staying visible.
    _reset()
    guvcfd_app._run_log("Running pimpleFoam to 60.0s")
    before = len(guvcfd_app._run_state["log"])
    for t in range(1, 21):
        guvcfd_app._track_solver_time(f"Time = {t}")
    assert len(guvcfd_app._run_state["log"]) == before


def test_format_mmss_below_an_hour():
    assert guvcfd_app._format_mmss(1088) == "18:08"
    assert guvcfd_app._format_mmss(5) == "0:05"


def test_format_mmss_over_an_hour():
    assert guvcfd_app._format_mmss(3661) == "1:01:01"


def test_progress_and_eta_are_separate_lines():
    _reset()
    guvcfd_app._run_log("Running pimpleFoam to 100.0s")
    guvcfd_app._track_solver_time("Time = 1")
    # Freeze phase_start_time 60s in the past so a rate is computable.
    guvcfd_app._run_state["phase_start_time"] -= 60
    guvcfd_app._track_solver_time("Time = 25")

    progress = guvcfd_app._solver_progress_text()
    eta = guvcfd_app._solver_eta_text()
    assert progress == "Simulation time step 25 of 100 (25%)"
    assert eta.startswith("Expected finish of this step in ")
    assert "ETA" not in progress
    assert "Simulation time step" not in eta
