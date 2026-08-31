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


def test_setup_case_sealed_without_fan_raises_before_any_wsl_call(monkeypatch):
    # A sealed room (ach<=0) has no driving force at all without a fan -
    # must fail fast (before touching WSL/OpenFOAM) rather than silently
    # building a case with no flow forcing whatsoever.
    def fail(*a, **k):
        raise AssertionError("must not reach WSL for a rejected sealed setup_case() call")

    monkeypatch.setattr(run_pipeline, "_run_wsl_or_raise", fail)
    monkeypatch.setattr(run_pipeline, "_run_wsl", fail)
    try:
        setup_case("unused.guv", "unused_dir", sealed=True, fan_speed=None)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "fan" in str(e).lower()


def test_finish_case_setup_mechanical_ach_only_skips_fluence_pipeline(monkeypatch):
    # mechanical_ach_only has nothing physical to bin (no fluence at all,
    # by user choice, independent of the project's actual lamp count) -
    # must never touch the fluence/inactivation-rate/cellZone/fvOptions
    # binning path, only write an empty fvOptions instead.
    def fail(*a, **k):
        raise AssertionError("must not touch the UV/fluence pipeline for mechanical_ach_only")

    for name in ("read_cell_centers", "compute_fluence_at_points", "read_boundary_patch_names",
                 "write_scalar_field", "compute_inactivation_rate", "compute_well_mixed_eACH",
                 "bin_decay_rates", "write_cellzones", "write_fvoptions"):
        monkeypatch.setattr(run_pipeline, name, fail)

    written = {}
    monkeypatch.setattr(run_pipeline, "write_fvoptions_file",
                         lambda case_dir, entries: written.setdefault("fvoptions", entries))
    monkeypatch.setattr(run_pipeline, "set_function_object_enabled", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "splice_fv_options_into_control_dict",
                         lambda case_dir: (f"{case_dir}/system/controlDict", 2, 2))
    monkeypatch.setattr(run_pipeline, "set_control_dict_time", lambda *a, **k: None)

    summary = {"ach": 3.0}
    result = run_pipeline._finish_case_setup(
        "unused_dir", room=None, Z=6.0, nbins=25, source_field="T", fan_entry=None,
        pimple_end_time=120, pimple_write_interval=10, pimple_delta_t=0.5, log_fn=lambda *a: None,
        summary=summary, mechanical_ach_only=True,
    )
    assert written["fvoptions"] == []
    assert result["eACH_uv_well_mixed_mean"] == 0.0
    assert result["n_zones"] == 0
    assert result["fluence_mean"] is None


def test_finish_case_setup_adaptive_t_relaxation_overrides_scalar_factor(monkeypatch):
    # When adaptive_t_relaxation=True, the T relaxation factor actually
    # written to fvSolution must be computed from THIS case's own
    # kUV.max (compute_adaptive_scalar_relaxation), not left at whatever
    # static value the template/caller supplied.
    import numpy as np

    monkeypatch.setattr(run_pipeline, "read_cell_centers", lambda case_dir, t: np.zeros((3, 3)))
    monkeypatch.setattr(run_pipeline, "compute_fluence_at_points", lambda room, pts: np.array([100.0, 200.0, 50.0]))
    monkeypatch.setattr(run_pipeline, "read_boundary_patch_names", lambda case_dir: [])
    monkeypatch.setattr(run_pipeline, "write_scalar_field", lambda *a, **k: None)
    # kUV = fluence * Z * 1e-3; Z=100 here makes kUV.max = 200*100*1e-3 = 20.0
    monkeypatch.setattr(run_pipeline, "compute_inactivation_rate", lambda values, Z: values * Z * 1e-3)
    monkeypatch.setattr(run_pipeline, "compute_well_mixed_eACH", lambda k_values: k_values)
    monkeypatch.setattr(run_pipeline, "bin_decay_rates", lambda k_values, nbins: (np.zeros(3, dtype=int), [0]))
    monkeypatch.setattr(run_pipeline, "write_cellzones", lambda case_dir, bin_idx, nbins: ([], None))
    monkeypatch.setattr(run_pipeline, "write_fvoptions", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "splice_fv_options_into_control_dict",
                         lambda case_dir: (f"{case_dir}/system/controlDict", 1, 1))
    monkeypatch.setattr(run_pipeline, "set_control_dict_time", lambda *a, **k: None)

    calls = []
    monkeypatch.setattr(run_pipeline, "set_relaxation_factors",
                         lambda case_dir, **kw: calls.append(kw))

    summary = {"ach": 6.0}
    result = run_pipeline._finish_case_setup(
        "unused_dir", room=None, Z=100.0, nbins=25, source_field="T", fan_entry=None,
        pimple_end_time=120, pimple_write_interval=10, pimple_delta_t=0.5, log_fn=lambda *a: None,
        summary=summary, mechanical_ach_only=False, adaptive_t_relaxation=True,
    )
    assert result["k_range"][1] == 20.0
    assert len(calls) == 1
    assert calls[0] == {"scalar_factor": run_pipeline.compute_adaptive_scalar_relaxation(20.0)}
    assert result["adaptive_scalar_relaxation"] == run_pipeline.compute_adaptive_scalar_relaxation(20.0)


def test_finish_case_setup_without_adaptive_flag_never_calls_set_relaxation_factors(monkeypatch):
    import numpy as np

    monkeypatch.setattr(run_pipeline, "read_cell_centers", lambda case_dir, t: np.zeros((3, 3)))
    monkeypatch.setattr(run_pipeline, "compute_fluence_at_points", lambda room, pts: np.array([100.0, 200.0, 50.0]))
    monkeypatch.setattr(run_pipeline, "read_boundary_patch_names", lambda case_dir: [])
    monkeypatch.setattr(run_pipeline, "write_scalar_field", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "compute_inactivation_rate", lambda values, Z: values * Z * 1e-3)
    monkeypatch.setattr(run_pipeline, "compute_well_mixed_eACH", lambda k_values: k_values)
    monkeypatch.setattr(run_pipeline, "bin_decay_rates", lambda k_values, nbins: (np.zeros(3, dtype=int), [0]))
    monkeypatch.setattr(run_pipeline, "write_cellzones", lambda case_dir, bin_idx, nbins: ([], None))
    monkeypatch.setattr(run_pipeline, "write_fvoptions", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "splice_fv_options_into_control_dict",
                         lambda case_dir: (f"{case_dir}/system/controlDict", 1, 1))
    monkeypatch.setattr(run_pipeline, "set_control_dict_time", lambda *a, **k: None)

    def fail(*a, **k):
        raise AssertionError("set_relaxation_factors must not be called when adaptive_t_relaxation=False")
    monkeypatch.setattr(run_pipeline, "set_relaxation_factors", fail)

    summary = {"ach": 6.0}
    result = run_pipeline._finish_case_setup(
        "unused_dir", room=None, Z=100.0, nbins=25, source_field="T", fan_entry=None,
        pimple_end_time=120, pimple_write_interval=10, pimple_delta_t=0.5, log_fn=lambda *a: None,
        summary=summary, mechanical_ach_only=False, adaptive_t_relaxation=False,
    )
    assert "adaptive_scalar_relaxation" not in result


def test_converge_flow_field_skip_potential_flow_never_runs_potentialfoam(monkeypatch, tmp_path):
    commands = []

    def fake_run_wsl(cmd, cwd_wsl):
        commands.append(cmd)
        if "endTime" in cmd:
            return SimpleNamespace(stdout="500", stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    def fake_run_wsl_or_raise(cmd, cwd_wsl, step_name):
        commands.append(cmd)
        if "[ -d" in cmd:
            return SimpleNamespace(stdout="yes", returncode=0)
        if "ls " in cmd and "grep" in cmd:
            return SimpleNamespace(stdout="U p k omega nut phi", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    def fake_run_wsl_streaming(cmd, cwd_wsl, on_line=None, should_stop=None, kill_pattern=None, should_pause=None):
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(run_pipeline, "_run_wsl", fake_run_wsl)
    monkeypatch.setattr(run_pipeline, "_run_wsl_or_raise", fake_run_wsl_or_raise)
    monkeypatch.setattr(run_pipeline, "_run_wsl_streaming", fake_run_wsl_streaming)
    monkeypatch.setattr(run_pipeline, "read_vol_average_dat", lambda path: ([0], [0.0]))
    monkeypatch.setattr(run_pipeline, "set_function_object_enabled", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "ensure_simple_fvsolution", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "write_fvoptions_file", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "set_control_dict_time", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "write_vol_average_dict", lambda *a, **k: None)

    # A single, never-converging chunk isn't enough evidence for the
    # oscillation-acceptance check either, so this legitimately raises
    # FlowConvergenceUndecided - irrelevant here, only whether
    # potentialFoam ran matters (that call happens before the loop).
    try:
        converge_flow_field(str(tmp_path), n_iterations=500, max_iterations=500, skip_potential_flow=True)
    except run_pipeline.FlowConvergenceUndecided:
        pass

    assert not any("potentialFoam" in c for c in commands)


def test_converge_flow_field_clears_stale_time_dirs_on_cold_start(monkeypatch, tmp_path):
    # Regression test for a real incident: re-running flow convergence
    # (cold start, not resume) on a case_dir that had already been through
    # a full Phase 1/Phase 2 left a stale, numerically-LARGER time
    # directory (steady_state_pipeline's own chunk loop deliberately keeps
    # its intermediate directories, unlike this loop's own end-of-chunk
    # cleanup) sitting there from that earlier, unrelated run. A cold start
    # must proactively clear it before the chunk loop's first check, not
    # just rely on the check itself being exact (belt and suspenders).
    commands = []

    def fake_run_wsl(cmd, cwd_wsl):
        commands.append(cmd)
        return SimpleNamespace(stdout="500" if "endTime" in cmd else "", stderr="", returncode=0)

    def fake_run_wsl_or_raise(cmd, cwd_wsl, step_name):
        commands.append(cmd)
        if "[ -d" in cmd:
            return SimpleNamespace(stdout="yes", returncode=0)
        if "ls " in cmd and "grep" in cmd:
            return SimpleNamespace(stdout="U p k omega nut phi", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(run_pipeline, "_run_wsl", fake_run_wsl)
    monkeypatch.setattr(run_pipeline, "_run_wsl_or_raise", fake_run_wsl_or_raise)
    monkeypatch.setattr(run_pipeline, "_run_wsl_streaming",
                         lambda *a, **k: SimpleNamespace(stdout="", returncode=0))
    monkeypatch.setattr(run_pipeline, "read_vol_average_dat", lambda path: ([0], [0.0]))
    monkeypatch.setattr(run_pipeline, "set_function_object_enabled", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "ensure_simple_fvsolution", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "write_fvoptions_file", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "set_control_dict_time", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "write_vol_average_dict", lambda *a, **k: None)

    try:
        converge_flow_field(str(tmp_path), n_iterations=500, max_iterations=500,
                             skip_potential_flow=True, log_fn=lambda *a: None)
    except FlowConvergenceUndecided:
        pass  # irrelevant here - only the cleanup/check commands matter

    # With max_iterations == n_iterations, exactly one chunk runs, so this
    # cleanup command (also issued once per chunk at its own end, an
    # existing, unrelated behavior) should appear TWICE here: once as this
    # cold start's own proactive clear before the loop even starts, once as
    # that one chunk's own end-of-loop cleanup - see the sibling resume
    # test, where only the latter fires.
    cleanup_cmds = [c for c in commands if "0/" in c and "rm -rf" in c and "for d in" in c]
    assert len(cleanup_cmds) == 2, (
        f"expected a cold-start clear plus the one chunk's own end-of-loop clear, got {cleanup_cmds!r}")
    # Never trusts "whichever directory is numerically largest" - the exact
    # bug that let a stale "5000/" masquerade as this chunk's own output.
    assert not any("sort -n" in c for c in commands)


def test_converge_flow_field_resume_does_not_clear_time_dirs(monkeypatch, tmp_path):
    # A resume's whole point is to keep prior chunks' own time directories
    # (that's what makes the resume meaningful) - must never issue the
    # cold-start cleanup.
    commands = []

    def fake_run_wsl(cmd, cwd_wsl):
        commands.append(cmd)
        return SimpleNamespace(stdout="500" if "endTime" in cmd else "", stderr="", returncode=0)

    def fake_run_wsl_or_raise(cmd, cwd_wsl, step_name):
        commands.append(cmd)
        if "[ -d" in cmd:
            return SimpleNamespace(stdout="yes", returncode=0)
        if "ls " in cmd and "grep" in cmd:
            return SimpleNamespace(stdout="U p k omega nut phi", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(run_pipeline, "_run_wsl", fake_run_wsl)
    monkeypatch.setattr(run_pipeline, "_run_wsl_or_raise", fake_run_wsl_or_raise)
    monkeypatch.setattr(run_pipeline, "_run_wsl_streaming",
                         lambda *a, **k: SimpleNamespace(stdout="", returncode=0))
    monkeypatch.setattr(run_pipeline, "read_vol_average_dat", lambda path: ([0], [0.0]))
    monkeypatch.setattr(run_pipeline, "set_function_object_enabled", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "ensure_simple_fvsolution", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "write_fvoptions_file", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "set_control_dict_time", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "write_vol_average_dict", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "_load_history", lambda case_dir: [])

    try:
        converge_flow_field(str(tmp_path), n_iterations=500, max_iterations=500,
                             resume=True, log_fn=lambda *a: None)
    except FlowConvergenceUndecided:
        pass

    # Only the one chunk's own end-of-loop cleanup fires (existing,
    # unrelated behavior) - no EXTRA cold-start clear on top of it, unlike
    # the sibling non-resume test above.
    cleanup_cmds = [c for c in commands if "0/" in c and "rm -rf" in c and "for d in" in c]
    assert len(cleanup_cmds) == 1, (
        f"resume must not add a cold-start clear on top of the one chunk's own cleanup, got {cleanup_cmds!r}")


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


def _patient_ward_settings():
    return {
        "inlet-wall": "xMin", "inlet-y-input": 1.5, "inlet-z-input": 2.1,
        "inlet-size-w": 0.3, "inlet-size-h": 0.3,
        "outlet-wall": "xMax", "outlet-y-input": 1.5, "outlet-z-input": 0.4,
        "outlet-size-w": 0.3, "outlet-size-h": 0.3,
        "inject-x-input": 2.0, "inject-y-input": 1.5, "inject-z-input": 1.5,
    }


def test_check_settings_grid_alignment_flags_the_real_shrinking_case():
    # Regression test for the real bug: this exact room/settings/cell_size
    # combination used to silently carve a 0.2x0.2m inlet and a
    # 0.3x0.2x0.2m source zone (both 44% of nominal) - now that the
    # underlying snap is outward-only, this should surface as a "will be
    # LARGER than typed" note, not a silent shortfall, and not a shrink.
    room = SimpleNamespace(x=3.2, y=4.8, z=2.57)
    settings = _patient_ward_settings()
    mismatches = run_pipeline.check_settings_grid_alignment(settings, room, cell_size=0.1)
    names = {m["name"] for m in mismatches}
    assert names == {"Inlet", "Outlet", "Contaminant source position"}
    for m in mismatches:
        if m["name"] == "Contaminant source position":
            # The source entry reports a CENTRE, not a size (2026-08-31): its
            # size is configured in whole cells and is exact by construction,
            # so the "never a shortfall" invariant below - which is about
            # opening SIZES snapping outward - does not apply. A suggested
            # centre legitimately moves either way along an axis.
            continue
        for nominal, actual in zip(m["nominal"], m["actual"]):
            assert actual >= nominal - 1e-9  # never a shortfall


def test_check_settings_grid_alignment_empty_when_already_grid_aligned():
    room = SimpleNamespace(x=4.0, y=4.0, z=4.0)
    settings = {
        "inlet-wall": "xMin", "inlet-y-input": 2.0, "inlet-z-input": 2.0,
        "inlet-size-w": 0.4, "inlet-size-h": 0.4,
        "outlet-wall": "xMax", "outlet-y-input": 2.0, "outlet-z-input": 2.0,
        "outlet-size-w": 0.4, "outlet-size-h": 0.4,
    }
    assert run_pipeline.check_settings_grid_alignment(settings, room, cell_size=0.1) == []


def test_check_settings_grid_alignment_skips_disabled_second_openings():
    room = SimpleNamespace(x=3.2, y=4.8, z=2.57)
    settings = dict(_patient_ward_settings())
    settings.update({
        "inlet2-enable": False, "inlet2-wall": "ceiling", "inlet2-y-input": 2.0, "inlet2-z-input": 1.5,
        "inlet2-size-w": 0.3, "inlet2-size-h": 0.3,
        "outlet2-enable": False, "outlet2-wall": "floor", "outlet2-y-input": 2.0, "outlet2-z-input": 1.5,
        "outlet2-size-w": 0.3, "outlet2-size-h": 0.3,
    })
    mismatches = run_pipeline.check_settings_grid_alignment(settings, room, cell_size=0.1)
    names = {m["name"] for m in mismatches}
    assert "2nd inlet" not in names and "2nd outlet" not in names


# --- walk_opening_alignment_conflicts (2026-08-07) ---

def _walk_all(settings, room, cell_size, agree_fn=lambda c: True):
    """Drive the generator to completion, returning the full list of
    conflicts it yielded, deciding each one via agree_fn(conflict)."""
    gen = run_pipeline.walk_opening_alignment_conflicts(settings, room, cell_size)
    seen = []
    conflict = next(gen, None)
    while conflict is not None:
        seen.append(conflict)
        try:
            conflict = gen.send(agree_fn(conflict))
        except StopIteration:
            conflict = None
    return seen


def test_walk_opening_alignment_conflicts_empty_when_already_aligned():
    room = SimpleNamespace(x=4.0, y=4.0, z=4.0)
    settings = {
        "inlet-wall": "xMin", "inlet-y-input": 2.0, "inlet-z-input": 2.0,
        "inlet-size-w": 0.4, "inlet-size-h": 0.4,
        "outlet-wall": "xMax", "outlet-y-input": 2.0, "outlet-z-input": 2.0,
        "outlet-size-w": 0.4, "outlet-size-h": 0.4,
    }
    assert _walk_all(settings, room, 0.1) == []


def test_walk_opening_alignment_conflicts_size_before_center_for_one_opening():
    room = SimpleNamespace(x=4.0, y=5.0, z=3.0)
    settings = {
        "inlet-wall": "xMin", "inlet-y-input": 1.37, "inlet-z-input": 0.68,
        "inlet-size-w": 0.23, "inlet-size-h": 0.35,
        "outlet-wall": "xMax", "outlet-y-input": 2.5, "outlet-z-input": 1.5,
        "outlet-size-w": 0.4, "outlet-size-h": 0.4,
    }
    conflicts = _walk_all(settings, room, 0.1)
    kinds = [(c["opening"], c["kind"], c["axis_label"]) for c in conflicts]
    assert kinds == [
        ("Inlet", "size", "width"), ("Inlet", "size", "height"),
        ("Inlet", "center", "position 1"), ("Inlet", "center", "position 2"),
    ]
    # Outlet is already exactly aligned - contributes nothing.


def test_walk_opening_alignment_conflicts_center_uses_the_agreed_size_not_nominal():
    room = SimpleNamespace(x=4.0, y=5.0, z=3.0)
    settings = {
        "inlet-wall": "xMin", "inlet-y-input": 1.37, "inlet-z-input": 0.68,
        "inlet-size-w": 0.23, "inlet-size-h": 0.35,
        "outlet-wall": "xMax", "outlet-y-input": 2.5, "outlet-z-input": 1.5,
        "outlet-size-w": 0.4, "outlet-size-h": 0.4,  # already aligned - contributes nothing
    }
    conflicts = _walk_all(settings, room, 0.1)
    center1 = next(c for c in conflicts if c["kind"] == "center" and c["axis_label"] == "position 1")
    # Matches the worked-example value computed against the AGREED 0.3m
    # width, not the original 0.23m nominal.
    assert abs(center1["suggested"] - 1.35) < 1e-9


def test_walk_opening_alignment_conflicts_declined_size_skips_its_own_center_check():
    room = SimpleNamespace(x=4.0, y=5.0, z=3.0)
    settings = {
        "inlet-wall": "xMin", "inlet-y-input": 1.37, "inlet-z-input": 0.68,
        "inlet-size-w": 0.23, "inlet-size-h": 0.35,
        "outlet-wall": "xMax", "outlet-y-input": 2.5, "outlet-z-input": 1.5,
        "outlet-size-w": 0.4, "outlet-size-h": 0.4,  # already aligned - contributes nothing
    }

    def decline_width_only(c):
        return not (c["kind"] == "size" and c["axis_label"] == "width")

    conflicts = _walk_all(settings, room, 0.1, agree_fn=decline_width_only)
    kinds = [(c["kind"], c["axis_label"]) for c in conflicts]
    assert ("size", "width") in kinds
    assert ("size", "height") in kinds
    assert ("center", "position 1") not in kinds  # width's size was declined -> skipped
    assert ("center", "position 2") in kinds       # height's size was agreed -> still checked


def test_walk_opening_alignment_conflicts_keeps_conflicts_correctly_attributed_per_opening():
    # Regression guard for the inlet/outlet mixup risk this session hit -
    # Inlet and Outlet have differently-shaped mismatches here, so a
    # cross-attribution bug would show up as a value from the wrong
    # opening's own fields.
    room = SimpleNamespace(x=4.0, y=5.0, z=3.0)
    settings = {
        "inlet-wall": "xMin", "inlet-y-input": 2.5, "inlet-z-input": 0.4,
        "inlet-size-w": 0.4, "inlet-size-h": 0.4,  # inlet: already aligned
        "outlet-wall": "xMax", "outlet-y-input": 2.35, "outlet-z-input": 2.7,
        "outlet-size-w": 0.4, "outlet-size-h": 0.2,  # outlet: needs a center shift
    }
    conflicts = _walk_all(settings, room, 0.1)
    assert len(conflicts) > 0  # sanity: this position genuinely needs a fix, not a vacuous pass
    assert all(c["opening"] == "Outlet" for c in conflicts)
    for c in conflicts:
        assert c["field_id"].startswith("outlet-")


def test_walk_opening_alignment_conflicts_disabled_second_openings_never_appear():
    room = SimpleNamespace(x=3.2, y=4.8, z=2.57)
    settings = dict(_patient_ward_settings())
    settings.update({
        "inlet2-enable": False, "inlet2-wall": "ceiling", "inlet2-y-input": 2.0, "inlet2-z-input": 1.5,
        "inlet2-size-w": 0.33, "inlet2-size-h": 0.33,
        "outlet2-enable": False, "outlet2-wall": "floor", "outlet2-y-input": 2.0, "outlet2-z-input": 1.5,
        "outlet2-size-w": 0.33, "outlet2-size-h": 0.33,
    })
    conflicts = _walk_all(settings, room, 0.1)
    assert all(c["opening"] not in ("2nd inlet", "2nd outlet") for c in conflicts)


def test_walk_opening_alignment_conflicts_enabled_second_openings_are_included():
    room = SimpleNamespace(x=3.2, y=4.8, z=2.57)
    settings = dict(_patient_ward_settings())
    settings.update({
        "inlet2-enable": True, "inlet2-wall": "ceiling", "inlet2-y-input": 2.0, "inlet2-z-input": 1.5,
        "inlet2-size-w": 0.33, "inlet2-size-h": 0.33,
        "outlet2-enable": False,
    })
    conflicts = _walk_all(settings, room, 0.1)
    assert any(c["opening"] == "2nd inlet" for c in conflicts)


def test_walk_opening_alignment_conflicts_never_yields_the_source_zone():
    room = SimpleNamespace(x=3.2, y=4.8, z=2.57)
    settings = _patient_ward_settings()  # inject-x/y/z-input present, but no source_size arg exists on this function
    conflicts = _walk_all(settings, room, 0.1)
    assert all("source" not in c["opening"].lower() for c in conflicts)


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
        if "endTime" in cmd:
            return SimpleNamespace(stdout="500", stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    def fake_run_wsl_or_raise(cmd, cwd_wsl, step_name):
        if "[ -d" in cmd:
            call_count["n"] += 1
            # Every chunk restarts from "0/" and targets endTime=n_iterations
            # again (by design - see converge_flow_field's own comment on
            # this), so the time directory reached is always n_iterations
            # (500 here), not a running cumulative total.
            return SimpleNamespace(stdout="yes", returncode=0)
        if "ls " in cmd and "grep" in cmd:
            return SimpleNamespace(stdout="U p k omega nut phi", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    def fake_run_wsl_streaming(cmd, cwd_wsl, on_line=None, should_stop=None, kill_pattern=None, should_pause=None):
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


def test_continue_flow_convergence_forwards_should_pause(monkeypatch, tmp_path):
    _save_history(str(tmp_path), _hist([0.15] * 3))

    captured = {}

    def fake_converge_flow_field(case_dir, **kwargs):
        captured.update(kwargs)
        return ("1500", True)

    monkeypatch.setattr(run_pipeline, "converge_flow_field", fake_converge_flow_field)
    should_pause = lambda: False
    continue_flow_convergence(str(tmp_path), additional_iterations=500, n_iterations=500,
                               log_fn=lambda *a: None, should_pause=should_pause)
    assert captured["should_pause"] is should_pause


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


def test_resume_case_setup_forwards_should_pause_to_continue_flow_convergence(monkeypatch, tmp_path):
    class _FakeRoom:
        x, y, z, units, lamps = 4.0, 6.0, 2.7, "meters", []

    class _FakeProject:
        rooms = {"a": _FakeRoom()}

        @staticmethod
        def load(path):
            return _FakeProject()

    monkeypatch.setattr(run_pipeline, "Project", _FakeProject)
    captured = {}

    def fake_continue_flow_convergence(case_dir, additional_iterations, **kwargs):
        captured.update(kwargs)
        return (None, True)
    monkeypatch.setattr(run_pipeline, "continue_flow_convergence", fake_continue_flow_convergence)
    monkeypatch.setattr(run_pipeline, "resolve_inlet_velocity", lambda *a, **k: (0.278, 0, 0))
    monkeypatch.setattr(run_pipeline, "restore_boundary_conditions", lambda *a, **k: None)
    monkeypatch.setattr(run_pipeline, "check_ach_delivery", lambda *a, **k: {})
    monkeypatch.setattr(run_pipeline, "_finish_case_setup", lambda *a, **k: {})

    should_pause = lambda: False
    resume_case_setup(str(tmp_path), "unused.guv", "continue", ach=1.5, Z=2.0,
                       additional_iterations=500, should_pause=should_pause)
    assert captured["should_pause"] is should_pause


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
