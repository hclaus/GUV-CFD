import json
import time

from guvcfd.qtapp.run_state import RunState, _settings_mismatch, launch_run, probe_resumable_state


def test_launch_run_stores_the_launched_z_and_ach_on_state(monkeypatch):
    # Regression for a real, reported bug: the Run tab's single-run
    # progress table displayed Z/ACH read from the Simulation Settings
    # dialog's own spinboxes, which can differ from what was actually
    # typed into the Run tab's own Z/ACH fields and launched - the solve
    # itself always used the correct values, but the table showed stale
    # ones. state.z/state.ach must reflect what launch_run was actually
    # given, set synchronously before the background worker thread starts,
    # so the caller (Run tab) can render from state instead of re-querying
    # a separate, possibly out-of-sync widget.
    import guvcfd.qtapp.run_state as run_state_module
    monkeypatch.setattr(run_state_module, "_run_steady_state", lambda *a, **k: None)

    state = RunState()
    settings = {"sim-type": "steady_state", "z-value": 4.0, "ach": 3.0}
    launch_run(state, "proj.guv", "case_dir", room=None, settings=settings)

    assert state.z == 4.0
    assert state.ach == 3.0

    for _ in range(50):
        if state.status != "running":
            break
        time.sleep(0.02)


def test_begin_chunked_phases_offsets_raw_time_within_a_phase():
    # Regression test for a real, confirmed incident: the "RUNNING NOW"
    # panel showed "Time step 130 of 12000" while the log's own narration
    # said the current chunk was "4401-4800 of 12000" - the raw, chunk-
    # local solver Time was being shown directly with no cumulative
    # offset, since each ~400-iteration chunk restarts OpenFOAM's own
    # "Time" counter at 0.
    state = RunState()
    state.begin_chunked_phases([(12000, 1.0), (8000, 1.0)])

    state.log_fn("Running simpleFoam (1-400 of 12000 iterations, writing every 200)...")
    state.solver_log_fn("Time = 130")
    assert state.current_time == "130.0"  # first chunk - no offset yet

    state.log_fn("Running simpleFoam (4401-4800 of 12000 iterations, writing every 200)...")
    state.solver_log_fn("Time = 130")
    # Same raw "Time = 130" as the first chunk, but this is the 12th chunk
    # (started at iteration 4401) - the displayed value must reflect true
    # cumulative progress (4400 + 130), not the chunk-local 130 alone.
    assert state.current_time == "4530.0"
    assert "4530" in state.progress_text()  # not "130" - the chunk-local raw value


def test_begin_chunked_phases_scales_offset_by_delta_t():
    # iteration_base is in ITERATION units (from the chunk-start narration
    # line), but raw solver "Time = N" is in OpenFOAM TIME units
    # (iteration * delta_t) - confirmed as a real, separate latent bug
    # found while fixing the above: adding an un-scaled iteration count
    # directly onto a delta_t-scaled Time would silently produce a wrong
    # cumulative value whenever residence-time deltaT scaling is active
    # (it happened to go unnoticed in the reported incident only because
    # that particular run's delta_t was exactly 1).
    state = RunState()
    state.begin_chunked_phases([(1000, 2.5)])

    state.log_fn("Running simpleFoam (401-800 of 1000 iterations, writing every 200)...")
    # 400 iterations already done, at delta_t=2.5 -> offset = 1000.0 (Time
    # units), not 400 (iteration units).
    state.solver_log_fn("Time = 50")
    assert state.current_time == "1050.0"
    # target_time must be in the same Time units too (1000 iterations *
    # delta_t=2.5 = 2500), not the bare iteration count.
    assert state.target_time == 2500.0


def test_begin_chunked_phases_transitions_from_phase1_to_phase2():
    state = RunState()
    state.begin_chunked_phases([(12000, 1.0), (2000, 1.0)])

    state.log_fn("Running simpleFoam (11601-12000 of 12000 iterations, writing every 200)...")
    state.solver_log_fn("Time = 400")
    assert state.current_time == "12000.0"

    state.log_fn("=== Phase 2: source + UV ===")
    # Phase 2's own target/offset must reset - continuing to add Phase 1's
    # leftover iteration_base would make Phase 2's progress read as
    # already-complete or over 100% from its very first chunk.
    assert state.target_time == 2000.0
    state.log_fn("Running simpleFoam (1-200 of 2000 iterations, writing every 200)...")
    state.solver_log_fn("Time = 50")
    assert state.current_time == "50.0"


def test_begin_chunked_phases_handles_phase1_checkpoint_skip_straight_to_phase2():
    # Regression guard for a real resume path (steady_state_pipeline's own
    # "Found a Phase 1 checkpoint from an earlier attempt - skipping Phase
    # 1 entirely and resuming straight into Phase 2"): Phase 1 never logs
    # a single chunk line in this case, so the transition can't be
    # inferred from chunk_start values at all - only the unconditional
    # "=== Phase 2: source + UV ===" narration reliably signals it.
    state = RunState()
    state.begin_chunked_phases([(12000, 1.0), (2000, 1.5)])

    state.log_fn("Found a Phase 1 checkpoint from an earlier attempt - skipping Phase 1 "
                 "entirely and resuming straight into Phase 2 with its already-converged state.")
    state.log_fn("=== Phase 2: source + UV ===")
    assert state.target_time == 3000.0  # 2000 * 1.5, Phase 2's own budget

    state.log_fn("Running simpleFoam (1-200 of 2000 iterations, writing every 200)...")
    state.solver_log_fn("Time = 75")
    assert state.current_time == "75.0"


def test_begin_chunked_phases_handles_phase2_resuming_mid_chunk():
    # A second real resume shape: Phase 2 itself resumes mid-way (not a
    # full skip), so its own FIRST logged chunk doesn't start at
    # chunk_start==1 - the "=== Phase 2" transition trigger must not
    # depend on that.
    state = RunState()
    state.begin_chunked_phases([(12000, 1.0), (2000, 1.0)])
    state.log_fn("=== Phase 2: source + UV ===")

    state.log_fn("Running simpleFoam (801-1000 of 2000 iterations, writing every 200)...")
    state.solver_log_fn("Time = 40")
    assert state.current_time == "840.0"


def test_begin_phase_still_works_unchanged_for_decay_mode():
    # begin_phase (decay's single-stage path, no chunk restarts) must stay
    # a plain, un-offset target - iteration_base/delta_t default to
    # 0.0/1.0 so solver_log_fn's offset math is a no-op.
    state = RunState()
    state.begin_phase(3600)
    state.solver_log_fn("Time = 1800")
    assert state.current_time == "1800.0"
    assert state.target_time == 3600


# --- probe_resumable_state / _settings_mismatch (2026-08-18 Continue/Resume feature) ---

def test_probe_resumable_state_none_for_missing_case_dir(tmp_path):
    assert probe_resumable_state(str(tmp_path / "does_not_exist"), "steady_state") is None


def test_probe_resumable_state_none_for_empty_case_dir(tmp_path):
    # Exists, but nothing has ever run in it - Start, not Continue.
    assert probe_resumable_state(str(tmp_path), "steady_state") is None


def test_probe_resumable_state_none_when_already_finished(tmp_path):
    (tmp_path / "0").mkdir()
    (tmp_path / "0" / "fluenceRate").write_text("")
    (tmp_path / "results.json").write_text("{}")
    assert probe_resumable_state(str(tmp_path), "steady_state") is None


def test_probe_resumable_state_phases_resumable_for_steady_state(tmp_path):
    # setup_case() fully finished (0/fluenceRate exists) but the scenario
    # itself never wrote results.json - resumable via setup_summary.json +
    # run_steady_state_scenario's own Phase 1/Phase 2 checkpoint/pending
    # auto-detection.
    (tmp_path / "0").mkdir()
    (tmp_path / "0" / "fluenceRate").write_text("")
    probe = probe_resumable_state(str(tmp_path), "steady_state")
    assert probe == {"stage": "phases", "resumable": True, "reason": ""}


def test_probe_resumable_state_phases_not_resumable_for_decay(tmp_path):
    # Regression guard for a real, confirmed gap found while building this:
    # continue_decay only extends an already-COMPLETE results.json further -
    # a decay run stopped before ever completing once has no resume path
    # today. Must report resumable=False with a real reason, not silently
    # mishandle it as if it were the steady-state case.
    (tmp_path / "0").mkdir()
    (tmp_path / "0" / "fluenceRate").write_text("")
    probe = probe_resumable_state(str(tmp_path), "decay")
    assert probe["stage"] == "phases"
    assert probe["resumable"] is False
    assert probe["reason"]  # a real, non-empty explanation


def test_probe_resumable_state_flow_stage_for_either_sim_type(tmp_path):
    # Flow convergence is shared setup_case() infrastructure, unaffected
    # by sim_type - resumable the same way for steady-state and decay.
    history = [{"iteration": 500, "value": 0.05}, {"iteration": 1000, "value": 0.0498}]
    (tmp_path / "flow_convergence_history.json").write_text(json.dumps(history))
    for sim_type in ("steady_state", "decay"):
        probe = probe_resumable_state(str(tmp_path), sim_type)
        assert probe == {"stage": "flow", "total_iterations": 1000}


def test_settings_mismatch_empty_when_no_run_settings_file(tmp_path):
    assert _settings_mismatch(str(tmp_path), {"ach": 6.0}) == []


def test_settings_mismatch_empty_when_nothing_mesh_affecting_changed(tmp_path):
    prior = {"ach": 6.0, "z-value": 2.0, "inlet-wall": "xMin"}
    (tmp_path / "run_settings.json").write_text(json.dumps(prior))
    current = dict(prior)
    current["monitor1-x-input"] = 999  # not mesh-affecting - must not trigger a mismatch
    assert _settings_mismatch(str(tmp_path), current) == []


def test_settings_mismatch_detects_a_changed_mesh_affecting_field(tmp_path):
    # Regression guard for the exact real-world risk this closes: resuming
    # with a DIFFERENT ACH/geometry than what the on-disk mesh/flow field
    # were actually built with would silently apply a mismatched setting.
    prior = {"ach": 6.0, "z-value": 2.0}
    (tmp_path / "run_settings.json").write_text(json.dumps(prior))
    current = {"ach": 9.0, "z-value": 2.0}
    mismatches = _settings_mismatch(str(tmp_path), current)
    assert mismatches == [("ach", 6.0, 9.0)]
