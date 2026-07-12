from guvcfd import app as guvcfd_app


def _reset():
    guvcfd_app._reset_run_progress("decay")


def test_flow_convergence_progress_is_cumulative_across_chunks():
    _reset()
    guvcfd_app._run_log("Flow-convergence budget: 5000 iterations max, in chunks of 500...")
    assert guvcfd_app._run_state["target_time"] == 5000.0

    guvcfd_app._run_log("Running simpleFoam iterations 1-500 (chunk size 500)...")
    for t in (1, 250, 500):
        guvcfd_app._run_log(f"Time = {t}")
    assert float(guvcfd_app._run_state["current_time"]) == 500.0

    # Second chunk: solver's own Time resets to ~0, but cumulative progress
    # must keep climbing past the previous chunk's end (500), not fall back.
    guvcfd_app._run_log("Running simpleFoam iterations 501-1000 (chunk size 500)...")
    guvcfd_app._run_log("Time = 1")
    assert float(guvcfd_app._run_state["current_time"]) == 501.0
    guvcfd_app._run_log("Time = 500")
    assert float(guvcfd_app._run_state["current_time"]) == 1000.0
    # target_time must not have been perturbed by the per-chunk line.
    assert guvcfd_app._run_state["target_time"] == 5000.0

    guvcfd_app._run_log("Running simpleFoam iterations 1001-1500 (chunk size 500)...")
    guvcfd_app._run_log("Time = 500")
    assert float(guvcfd_app._run_state["current_time"]) == 1500.0
    assert guvcfd_app._run_state["target_time"] == 5000.0


def test_progress_text_climbs_monotonically_across_chunks():
    _reset()
    guvcfd_app._run_log("Flow-convergence budget: 1500 iterations max, in chunks of 500...")
    pcts = []
    for start in (1, 501, 1001):
        guvcfd_app._run_log(f"Running simpleFoam iterations {start}-{start + 499} (chunk size 500)...")
        guvcfd_app._run_log("Time = 500")
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
    guvcfd_app._run_log("Time = 30")
    assert guvcfd_app._run_state["current_time"] == "30"
    assert guvcfd_app._run_state["chunk_base"] is None
