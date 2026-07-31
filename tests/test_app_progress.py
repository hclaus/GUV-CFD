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
                "eACH_uv_effective_corrected": 34.7,
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
# Measured ACH eff. %, Measured UV eff. %, Est. ACH /hr, Est. eACH /hr -
# see app._scenario_progress_table.
_EACH_COL = 8


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
