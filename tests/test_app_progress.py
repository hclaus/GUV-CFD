import dash
import pytest

from guvcfd import app as guvcfd_app


def _reset():
    guvcfd_app._reset_run_progress("decay")


def test_decision_panel_text_handles_flow_convergence_diagnostic():
    # _oscillation_diagnostic's shape (chunks_available/
    # chunks_needed_for_oscillation_check/trend/last_chunk_rel_change).
    decision = {"total_iterations": 1500, "kind": "flow"}
    diagnostic = {
        "summary": "trending toward agreement", "chunks_available": 3,
        "chunks_needed_for_oscillation_check": 8, "trend": "growing",
        "last_chunk_rel_change": 0.021, "rel_tol": 0.01, "chunk_size": 500,
    }
    text = guvcfd_app._decision_panel_text(decision, diagnostic)
    assert "1500 iterations" in text
    assert "Chunks available: 3" in text
    assert "2.1%" in text


def test_decision_panel_text_handles_phase1_extrapolation_diagnostic_without_crashing():
    # Regression: _phase1_extrapolation_diagnostic's dict has a completely
    # different shape (no chunks_available/chunks_needed_for_oscillation_
    # check/trend/last_chunk_rel_change - those are _oscillation_diagnostic-
    # only keys) - the panel-rendering code used to always assume the flow
    # shape regardless of decision["kind"], raising a KeyError the first
    # time a real Phase1ExtrapolationUndecided pause was rendered live.
    decision = {"total_iterations": 1500, "kind": "phase1_extrapolation"}
    diagnostic = {
        "summary": "still 3.2% apart (target <= 2%) - trending toward agreement, just not stable enough yet.",
        "n_attempts": 4, "n_successful_fits": 3, "recent_estimates": [61.2, 60.8, 59.9],
        "streak_required": 3, "rel_tol": 0.02, "chunk_size": 400, "n_iterations": 1500,
    }
    text = guvcfd_app._decision_panel_text(decision, diagnostic)
    assert "1500 iterations" in text
    assert "trending toward agreement" in text
    assert "chunks_available" not in text.lower()  # never touches flow-only keys


def test_phase1_decision_iterations_suggestion_uses_chunk_size_times_streak():
    diagnostic = {"chunk_size": 400, "streak_required": 3}
    assert guvcfd_app._phase1_decision_iterations_suggestion(diagnostic) == 1200


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


def test_phase_target_pattern_matches_current_steady_state_log_line():
    # Regression: _PHASE_TARGET_PATTERNS' steady-state entry used to match
    # "Running simpleFoam (N iterations, writing every" - _run_phase's own
    # current wording is "Running simpleFoam (801-1200 of 1500 iterations,
    # writing every 200)...", which that old pattern never matched at all,
    # silently leaving target_time (and therefore the whole ETA/progress-
    # pct text) unset for every steady-state Phase 1/2 run.
    _reset()
    guvcfd_app._run_log("Running simpleFoam (801-1200 of 1500 iterations, writing every 200)...")
    assert guvcfd_app._run_state["target_time"] == 1500.0


def test_phase_target_pattern_matches_single_run_concurrent_decay_launch():
    # Regression: _finish_decay's "Running pimpleFoam concurrently: UV-on
    # (Xs) + UV-off control (Ys)..." announcement (the normal, non-Continue
    # single-run decay path - see _run_decay_pair) matched no pattern at
    # all - target_time/phase_start_time/chunk_base silently kept
    # whatever flow convergence left them at, freezing "Running now" at a
    # stale flow-convergence figure for the rest of the run even though
    # the log itself kept scrolling live "Time = N" lines throughout.
    _reset()
    guvcfd_app._run_state["chunk_base"] = 1049.0  # stale, left over from flow convergence
    guvcfd_app._run_log("Running pimpleFoam concurrently: UV-on (640s) + UV-off control (1382s)...")
    assert guvcfd_app._run_state["target_time"] == 640.0
    assert guvcfd_app._run_state["chunk_base"] is None
    guvcfd_app._track_solver_time("Time = 5")
    assert guvcfd_app._run_state["current_time"] == "5"  # not offset by the stale chunk_base anymore


def test_phase_target_patterns_match_scenario_runs_control_and_uvon_messages():
    # scenario_runs._run_shared_control/_run_scenario log their own,
    # differently-worded equivalent of the single-run decay line ("Running
    # pimpleFoam to Xs") - both need their own pattern for sweep-mode ETA
    # tracking to work at all.
    progress = guvcfd_app._new_progress_entry()
    guvcfd_app._update_progress_from_log_line(progress, "  Running pimpleFoam (shared control, 1382s)...")
    assert progress["target_time"] == 1382.0

    progress = guvcfd_app._new_progress_entry()
    guvcfd_app._update_progress_from_log_line(progress, "  Running pimpleFoam (UV-on, 640s)...")
    assert progress["target_time"] == 640.0


def _reset_scenario():
    guvcfd_app._scenario_state.update(live_status={}, progress={})


def test_scenario_log_populates_progress_from_prefixed_narration_line():
    _reset_scenario()
    guvcfd_app._scenario_log("[ACH=6] Running simpleFoam (1-500 of 1500 iterations, writing every 200)...")
    assert guvcfd_app._scenario_state["progress"]["ACH=6"]["target_time"] == 1500.0


def test_scenario_log_ignores_unprefixed_lines():
    _reset_scenario()
    guvcfd_app._scenario_log("Stopped: user requested stop")
    assert guvcfd_app._scenario_state["progress"] == {}


def test_scenario_status_update_populates_current_time_under_stripped_prefix():
    # status_key is always log_prefix + "/" + a phase suffix (see
    # _scenario_status_update's own docstring) - current_time must land
    # under the SAME key _scenario_log's own prefix uses, so a combo's
    # target_time (from the log line) and current_time (from the solver
    # status line) end up in one entry an ETA can be computed from.
    _reset_scenario()
    guvcfd_app._scenario_status_update("Z=6/ACH=6/Phase2", "Time = 42")
    assert guvcfd_app._scenario_state["progress"]["Z=6/ACH=6"]["current_time"] == "42"


def test_scenario_status_update_none_clears_the_progress_entry():
    _reset_scenario()
    guvcfd_app._scenario_status_update("Z=6/ACH=6/Phase2", "Time = 42")
    guvcfd_app._scenario_status_update("Z=6/ACH=6/Phase2", None)
    assert "Z=6/ACH=6" not in guvcfd_app._scenario_state["progress"]


def test_combo_live_stage_finds_shared_phase1_key_for_every_z():
    # Regression: Phase 1 is ACH-group-shared (see scenario_runs.
    # _run_shared_phase1) - its status key ("ACH=6/Phase1") never carries a
    # Z at all, unlike Phase2/UV-on's per-combo "Z={z}/ACH={ach}/..." keys.
    # Before this was special-cased, only whichever Z happened to equal the
    # (irrelevant, placeholder) Z run_steady_state_scenario was called with
    # would coincidentally match - every other Z sharing that ACH silently
    # showed no stage at all despite Phase 1 actively running on its
    # behalf too.
    _reset_scenario()
    guvcfd_app._scenario_state["live_status"]["ACH=6/Phase1"] = "Time = 500"
    assert guvcfd_app._combo_live_stage(1.7, 6) == "Phase 1"
    assert guvcfd_app._combo_live_stage(99, 6) == "Phase 1"  # any Z sharing this ACH, not just one


def test_combo_eta_text_end_to_end_via_scenario_log_and_status_update():
    _reset_scenario()
    guvcfd_app._scenario_log("[Z=6/ACH=6] Running simpleFoam (1-500 of 2000 iterations, writing every 200)...")
    guvcfd_app._scenario_state["live_status"]["Z=6/ACH=6/Phase2"] = "Time = 500"
    # Freeze phase_start_time 60s in the past so a rate is computable (see
    # test_progress_and_eta_are_separate_lines' own use of this trick).
    guvcfd_app._scenario_state["progress"]["Z=6/ACH=6"]["phase_start_time"] -= 60
    guvcfd_app._scenario_status_update("Z=6/ACH=6/Phase2", "Time = 500")

    assert guvcfd_app._combo_eta_text(6, 6) != ""
    assert guvcfd_app._combo_eta_text(1.7, 6) == ""  # a different combo, nothing running for it


def test_eta_text_from_progress_blank_when_incomplete():
    assert guvcfd_app._eta_text_from_progress(None) == ""
    assert guvcfd_app._eta_text_from_progress({}) == ""
    assert guvcfd_app._eta_text_from_progress(
        {"current_time": "10", "target_time": None, "phase_start_time": 1.0}) == ""


def test_eta_text_from_progress_almost_done_when_already_past_target():
    import time as _time
    progress = {"current_time": "100", "target_time": 50.0, "phase_start_time": _time.time() - 10}
    assert guvcfd_app._eta_text_from_progress(progress) == "almost done"


def test_scenario_progress_table_handles_decay_mode_result():
    # Regression: _scenario_progress_table hardcoded detail['reduction_pct']/
    # detail['eACH_uv_steady_state'] - steady-state-only field names - and
    # crashed with KeyError on any decay-mode scenario combo's own trimmed
    # result shape (_trim_decay_report), which has neither.
    guvcfd_app._scenario_state["combos"] = [(6.0, 3.0)]
    guvcfd_app._scenario_state["results"] = {
        (6.0, 3.0): {
            "status": "done",
            "detail": {
                "eACH_uv_actual": 34.7,
                "ventilation_ach_measured": 4.2,
                "eACH_uv_well_mixed": 72.3,
            },
        },
    }
    table = guvcfd_app._scenario_progress_table()  # must not raise
    assert table is not None


def test_scenario_progress_table_still_handles_steady_state_result():
    guvcfd_app._scenario_state["combos"] = [(6.0, 3.0)]
    guvcfd_app._scenario_state["results"] = {
        (6.0, 3.0): {
            "status": "done",
            "detail": {"reduction_pct": 74.7, "eACH_uv_steady_state": 17.73},
        },
    }
    table = guvcfd_app._scenario_progress_table()  # must not raise
    assert table is not None


def _row_cell_texts(table, row_index):
    row = table.children[row_index]
    return [cell.children for cell in row.children]


# Column order: Z, ACH, Status, Est. time to finish, Total reduction %,
# Measured ACH eff. %, Measured UV eff. %, Mechanical mixing eff. %,
# Est. ACH /hr, Est. eACH /hr - see app._scenario_progress_table.
_EACH_COL = 9


def test_scenario_progress_table_prefers_corrected_eACH_for_steady_state():
    # Regression: this column showed detail['eACH_uv_steady_state'] (the
    # NOMINAL-ACH-based value) unconditionally, while the decay-mode branch
    # right below it already preferred the measured-ACH-corrected variant -
    # so the two sim types' tables were reporting different quantities.
    # Confirmed on a real run where a room only delivering ~half its nominal
    # ACH showed 39.24 /hr (nominal) here but 19.59 /hr (corrected, matching
    # decay's own reporting convention) in the exported .docx report.
    guvcfd_app._scenario_state["combos"] = [(1.7, 9.0)]
    guvcfd_app._scenario_state["results"] = {
        (1.7, 9.0): {
            "status": "done",
            "detail": {
                "reduction_pct": 81.34,
                "eACH_uv_steady_state": 39.24,
                "eACH_uv_steady_state_corrected": 19.59,
                "ventilation_ach_measured": 4.49,
            },
        },
    }
    table = guvcfd_app._scenario_progress_table()
    cells = _row_cell_texts(table, 1)
    assert cells[_EACH_COL] == "19.59 /hr"


def test_scenario_progress_table_falls_back_to_raw_eACH_when_uncorrected():
    # No control/measured-ACH data available (e.g. monitoring/control run
    # wasn't part of this sweep) - falls back to the nominal value rather
    # than crashing or showing nothing.
    guvcfd_app._scenario_state["combos"] = [(6.0, 3.0)]
    guvcfd_app._scenario_state["results"] = {
        (6.0, 3.0): {
            "status": "done",
            "detail": {"reduction_pct": 74.7, "eACH_uv_steady_state": 17.73},
        },
    }
    table = guvcfd_app._scenario_progress_table()
    cells = _row_cell_texts(table, 1)
    assert cells[_EACH_COL] == "17.73 /hr"


def test_write_single_run_summary_csv(tmp_path):
    import csv as csv_module
    import json as json_module
    case_dir = str(tmp_path)
    results = {
        "reduction_pct_corrected": 85.8, "eACH_uv_steady_state_corrected": 18.2,
        "eACH_uv_well_mixed": 20.0, "ach_delivery": {"measured_ach": 2.97, "ratio": 0.99},
    }
    (tmp_path / "results.json").write_text(json_module.dumps(results))
    guvcfd_app._run_state["z"] = 1.7
    guvcfd_app._run_state["ach"] = 3.0

    guvcfd_app._write_single_run_summary_csv(case_dir)

    csv_path = tmp_path / "run_summary.csv"
    assert csv_path.exists()
    with open(csv_path, newline="") as f:
        rows = list(csv_module.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["Z"] == "1.7"
    assert rows[0]["ACH"] == "3.0"
    assert float(rows[0]["total_reduction_pct"]) == pytest.approx(85.8)


def test_write_single_run_summary_csv_missing_results_json_is_a_noop(tmp_path):
    guvcfd_app._write_single_run_summary_csv(str(tmp_path))  # must not raise
    assert not (tmp_path / "run_summary.csv").exists()


def test_resync_pollers_reenables_both_for_an_active_single_run():
    # Regression: a mid-run page refresh resets run-poll/scenario-poll's
    # own "disabled" prop back to its layout default (True) - _run_state
    # itself is untouched (it's server-side, independent of any browser
    # session - confirmed directly: a live decay run's pimpleFoam processes
    # kept computing normally through a refresh that left the UI blank),
    # but with nothing to re-enable the poller, the page never resyncs on
    # its own. Single-run mode needs BOTH re-enabled: scenario-poll drives
    # the actual visible rendering (see _poll_scenario), but the decision
    # panels are only ever toggled by run-poll (see _poll_run).
    _reset()
    _reset_scenario()
    guvcfd_app._run_state["status"] = "awaiting_decision"
    guvcfd_app._scenario_state["status"] = "idle"
    run_poll_disabled, scenario_poll_disabled = guvcfd_app._resync_pollers(1)
    assert run_poll_disabled is False
    assert scenario_poll_disabled is False


def test_resync_pollers_reenables_only_scenario_poll_for_an_active_sweep():
    _reset()
    _reset_scenario()
    guvcfd_app._run_state["status"] = "idle"
    guvcfd_app._scenario_state["status"] = "running"
    run_poll_disabled, scenario_poll_disabled = guvcfd_app._resync_pollers(1)
    assert run_poll_disabled is dash.no_update
    assert scenario_poll_disabled is False


def test_resync_pollers_does_nothing_when_nothing_is_running():
    _reset()
    _reset_scenario()
    guvcfd_app._run_state["status"] = "idle"
    guvcfd_app._scenario_state["status"] = "idle"
    run_poll_disabled, scenario_poll_disabled = guvcfd_app._resync_pollers(1)
    assert run_poll_disabled is dash.no_update
    assert scenario_poll_disabled is dash.no_update


def test_current_stage_label_collapses_checklist_step_to_shared_vocabulary():
    # "Running" alone didn't distinguish setup/flow-convergence/the actual
    # measurement - _current_stage_label derives a meaningful label from
    # whichever checklist step _run_log's own marker logic has already
    # marked "running", collapsed into Setup/Flow field calc/Decay sim/
    # Phase 1/Phase 2/Post-processing.
    _reset()
    guvcfd_app._run_state["steps"] = guvcfd_app.DECAY_STEPS
    guvcfd_app._run_state["step_status"] = {s: "pending" for s in guvcfd_app.DECAY_STEPS}
    guvcfd_app._run_state["step_status"]["Converge flow field"] = "running"
    assert guvcfd_app._current_stage_label() == "Flow field calc"

    guvcfd_app._run_state["step_status"]["Converge flow field"] = "done"
    guvcfd_app._run_state["step_status"]["Run pimpleFoam (decay)"] = "running"
    assert guvcfd_app._current_stage_label() == "Decay sim"


def test_current_stage_label_falls_back_to_running_with_no_steps():
    _reset()
    guvcfd_app._run_state["steps"] = []
    guvcfd_app._run_state["step_status"] = {}
    assert guvcfd_app._current_stage_label() == "Running"


def test_combo_live_stage_labels_uvon_as_decay_sim_not_phase_2():
    # Regression: UV-on's own status-key suffix used to map to the "Phase
    # 2" label, meaningless for decay mode (which has no phases at all) -
    # fixed to "Decay sim".
    _reset_scenario()
    guvcfd_app._scenario_state["live_status"]["Z=6/ACH=6/UV-on"] = "Time = 100"
    assert guvcfd_app._combo_live_stage(6, 6) == "Decay sim"


def test_with_total_run_time_prepends_line_and_freezes_once_stopped():
    import time as _time
    text = guvcfd_app._with_total_run_time(_time.time() - 65, "Finished. 1/1 succeeded.")
    assert isinstance(text, list)
    assert text[0].startswith("Total run time: 1:0")  # ~65s -> "1:05"ish
    assert text[-1] == "Finished. 1/1 succeeded."


def test_with_total_run_time_passes_through_when_no_start_time():
    assert guvcfd_app._with_total_run_time(None, "Running...") == "Running..."


def test_poll_scenario_shows_full_sweep_table_not_stale_single_run_after_finishing():
    # Regression: a finished multi-combo sweep's own table used to
    # collapse down to a single stale row whenever ANY earlier single test
    # run (in this session) had left _run_state non-idle - _scenario_state's
    # own genuine multi-combo results were completely hidden (the branch
    # below only checked "_run_state isn't idle", not "which of the two is
    # actually the more recent one"). Confirmed live: a 4-combo sweep's
    # table shrank to 1 row right when the sweep finished. See
    # _poll_scenario's run_is_more_recent comment for the mechanism.
    _reset()
    _reset_scenario()
    # An earlier, unrelated single test run that finished FIRST.
    guvcfd_app._run_state.update(status="done", case_dir="/some/old/case", z=6.0, ach=3.0,
                                  start_time=1000.0)
    # A genuine multi-combo sweep, launched and finished LATER.
    guvcfd_app._scenario_state.update(
        status="done", combos=[(6.0, 3.0), (6.0, 6.0), (6.0, 9.0), (8.5, 3.0)], results={},
        start_time=2000.0, log=[], live_status={})

    table = guvcfd_app._poll_scenario(1)[3]
    assert len(table.children) == 5  # header + 4 combo rows - the real sweep table, not 1 stale row


def test_poll_scenario_still_shows_single_run_table_when_that_is_the_latest_action():
    # The fix must not break the genuine 1-combination-run case (see
    # _start_scenario_sweep) - _run_state should still win when it's
    # actually the most recently launched thing (no sweep has run since).
    _reset()
    _reset_scenario()
    guvcfd_app._run_state.update(status="done", case_dir="/some/case", z=6.0, ach=3.0,
                                  start_time=2000.0)
    guvcfd_app._scenario_state.update(status="idle", combos=[], results={}, start_time=None, log=[],
                                       live_status={})

    table = guvcfd_app._poll_scenario(1)[3]
    assert len(table.children) == 2  # header + 1 row - the single-run table


def _minimal_decay_settings_values(case_dir):
    """One value per guvcfd_app.SETTINGS_FIELDS entry, in order - a
    minimal but _validate_settings-passing decay-mode configuration
    (fan/inlet2/outlet2/monitoring all disabled, so none of their
    conditionally-required fields matter)."""
    values = {
        "project-description": "", "case-dir": str(case_dir), "ach": 6.0, "z-value": 6.0,
        "inlet-show": True, "inlet-wall": "xMin", "inlet-y-input": 1.5, "inlet-z-input": 2.1,
        "inlet-size-w": 0.4, "inlet-size-h": 0.4, "inlet-diffuser-type": "direct",
        "outlet-show": True, "outlet-wall": "xMax", "outlet-y-input": 1.5, "outlet-z-input": 0.4,
        "outlet-size-w": 0.4, "outlet-size-h": 0.4,
        "inlet2-enable": False, "inlet2-wall": "ceiling", "inlet2-y-input": 2.0, "inlet2-z-input": 1.5,
        "inlet2-size-w": 0.3, "inlet2-size-h": 0.3, "inlet2-diffuser-type": "direct",
        "outlet2-enable": False, "outlet2-wall": "floor", "outlet2-y-input": 2.0, "outlet2-z-input": 1.5,
        "outlet2-size-w": 0.3, "outlet2-size-h": 0.3,
        "fan-enable": False, "fan-speed": 0.3, "fan-direction": "down", "fan-radius": 0.6,
        "fan-thickness": 0.2, "fan-x-input": 2.0, "fan-y-input": 1.5, "fan-z-input": 2.2,
        "sim-type": "decay", "mech-ach-only": False, "pimple-end-time": 60, "pimple-write-interval": 5,
        "inject-x-input": 2.0, "inject-y-input": 1.5, "inject-z-input": 1.5, "source-zone-cells": 1, "breathing-velocity": 0.06,
                "breathing-dir-x": 0.0, "breathing-dir-y": 0.0, "breathing-dir-z": 1.0,
        "phase1-iterations": 1000, "phase2-iterations": 1000, "t-ss-window-frac": 0.15,
        "deltat-scaling-enabled": True, "deltat-effective-fraction": 0.7, "deltat-target-fraction": 0.995,
        "scenario-z-values": "6,8.5", "scenario-ach-values": "3,6",
        "monitoring-enable": False,
    }
    for i in (1, 2, 3):
        values.update({
            f"monitor{i}-enable": False, f"monitor{i}-name": f"Point {i}",
            f"monitor{i}-x-input": 2.0, f"monitor{i}-y-input": 1.5, f"monitor{i}-z-input": 1.5,
            f"monitor{i}-cells": 4,
        })
    return [values[fid] for fid in guvcfd_app.SETTINGS_FIELDS]


def test_start_scenario_sweep_clears_stale_results_for_a_genuine_multi_combo_sweep(tmp_path, monkeypatch):
    # Regression: nothing ever cleared results-data/results-case-dir when a
    # genuine multi-combo sweep launched - only the 1-combination path (via
    # _poll_run) ever auto-loads its own result, so a result loaded/auto-
    # loaded from any EARLIER, unrelated run just sat there through and
    # after the sweep, showing on the Analysis tab as if it belonged to the
    # sweep that just finished. Confirmed live ("shows results from a run
    # that has nothing to do with the sweep").
    _reset()
    _reset_scenario()
    monkeypatch.setattr(guvcfd_app, "_launch_scenario_sweep", lambda *a, **k: None)
    guvcfd_app._loaded["room"] = object()
    guvcfd_app._loaded["path"] = "dummy.guv"

    values = _minimal_decay_settings_values(tmp_path)
    result = guvcfd_app._start_scenario_sweep(1, "6,8.5", "3,6", *values)  # 4 combinations
    assert result[-2:] == (None, None)


def test_start_scenario_sweep_leaves_results_alone_for_a_1_combo_run(tmp_path, monkeypatch):
    # The 1-combination path reuses _run_state/_launch_run, which already
    # auto-loads its own result once finished (see _poll_run) - clearing
    # here would just create a pointless blank flash right before that.
    _reset()
    _reset_scenario()
    monkeypatch.setattr(guvcfd_app, "_launch_run", lambda *a, **k: None)

    def _no_pending(*a, **k):
        return None

    monkeypatch.setattr(guvcfd_app, "case_awaiting_flow_decision", _no_pending)
    monkeypatch.setattr(guvcfd_app, "case_awaiting_phase2_resume", _no_pending)
    guvcfd_app._loaded["room"] = object()
    guvcfd_app._loaded["path"] = "dummy.guv"

    values = _minimal_decay_settings_values(tmp_path / "case")
    result = guvcfd_app._start_scenario_sweep(1, "6", "3", *values)  # exactly 1 combination
    assert result[-2:] == (dash.no_update, dash.no_update)
