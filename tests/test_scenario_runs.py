import json
import threading
import time

import numpy as np
import pytest

import guvcfd.scenario_runs as sr
from guvcfd.case_io import read_openfoam_scalar_field, write_scalar_field
from guvcfd.wsl_utils import StoppedByUser


def test_sweep_combinations_is_full_cross_product_ach_major():
    combos = sr.sweep_combinations([6, 2], [3, 6])
    assert combos == [(2, 3), (6, 3), (2, 6), (6, 6)]


def test_sweep_combinations_dedups():
    combos = sr.sweep_combinations([2, 2, 6], [3, 3])
    assert combos == [(2, 3), (6, 3)]


@pytest.mark.parametrize("z,ach,expected", [
    (6, 3, "Z6_ACH3"),
    (2.5, 4.5, "Z2.5_ACH4.5"),
    (0.001, 100, "Z0.001_ACH100"),
])
def test_subdir_name_formatting(z, ach, expected):
    assert sr._subdir_name(z, ach) == expected


def test_sanitize_strips_unsafe_characters():
    assert sr._sanitize("a/b\\c:d") == "a_b_c_d"
    assert sr._sanitize("") == "case"


def test_trim_report_strips_bulky_arrays_keeps_everything_else():
    result = {
        "reduction_pct": 94.1,
        "eACH_uv_steady_state": 95.9,
        "phase1": {"T_ss": 2.0, "converged": True, "live": {"t": [1, 2], "T": [0.1, 0.2]},
                   "decay_curve": {"t": [1], "T": [0.1]}},
        "phase2": {"T_ss": 0.1, "converged": True, "live": {"t": [1, 2], "T": [0.1, 0.2]},
                   "decay_curve": {"t": [1], "T": [0.1]}},
        "monitoring": {
            "Patient": {
                "phase1": {"T_ss": 0.5, "t_seconds": [1, 2], "volAverage_T": [0.1, 0.2]},
                "phase2": {"T_ss": 0.05, "t_seconds": [1, 2], "volAverage_T": [0.1, 0.2]},
            },
        },
    }
    trimmed = sr._trim_report(result)

    assert trimmed["reduction_pct"] == 94.1
    assert trimmed["eACH_uv_steady_state"] == 95.9
    assert trimmed["phase1"]["T_ss"] == 2.0
    assert "live" not in trimmed["phase1"]
    assert "decay_curve" not in trimmed["phase1"]
    assert "live" not in trimmed["phase2"]
    assert trimmed["monitoring"]["Patient"]["phase1"]["T_ss"] == 0.5
    assert "t_seconds" not in trimmed["monitoring"]["Patient"]["phase1"]
    assert "volAverage_T" not in trimmed["monitoring"]["Patient"]["phase1"]
    # original untouched
    assert "live" in result["phase1"]


def test_trim_report_handles_missing_phases_and_monitoring():
    result = {"reduction_pct": 50.0}
    trimmed = sr._trim_report(result)
    assert trimmed == {"reduction_pct": 50.0}


def _write_synthetic_case(tmp_path, n_cells=4):
    case_dir = tmp_path / "case"
    (case_dir / "0").mkdir(parents=True)
    (case_dir / "system").mkdir(parents=True)
    poly = case_dir / "constant" / "polyMesh"
    poly.mkdir(parents=True)
    (poly / "boundary").write_text("""FoamFile
{
    version     2.0;
    format      ascii;
    class       polyBoundaryMesh;
    object      boundary;
}

1
(
    outlet
    {
        type            patch;
        nFaces          0;
        startFace       0;
    }
)
""")
    fluence = np.array([1.0, 2.0, 3.0, 4.0][:n_cells])
    write_scalar_field(str(case_dir), "fluenceRate", fluence, ["outlet"])
    return str(case_dir), fluence


def test_apply_z_writes_kuv_matching_fluence_and_z(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no WSL call expected without a fan")))
    case_dir, fluence = _write_synthetic_case(tmp_path)

    summary = sr._apply_z(case_dir, Z=2.0, nbins=5, fan_kwargs={}, log_fn=lambda m: None)

    k_values = read_openfoam_scalar_field(f"{case_dir}/0/kUV")
    expected = 2.0 * fluence * 1e-3  # matches fluence.compute_inactivation_rate's unit conversion
    assert np.allclose(k_values, expected, rtol=1e-3)
    assert summary["fluence_mean"] == pytest.approx(fluence.mean())
    assert (tmp_path / "case" / "constant" / "polyMesh" / "cellZones").exists()


def test_apply_z_recarves_fan_zone_when_fan_enabled(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, *a, **k: calls.append(cmd))
    case_dir, _ = _write_synthetic_case(tmp_path)

    fan_kwargs = {"fan_center": (1.0, 1.0, 1.0), "fan_disk_thickness": 0.2, "fan_disk_radius": 0.6}
    sr._apply_z(case_dir, Z=2.0, nbins=5, fan_kwargs=fan_kwargs, log_fn=lambda m: None)

    assert len(calls) == 1
    assert "topoSet" in calls[0]


def test_run_sweep_creates_expected_subfolders_and_reports(tmp_path, monkeypatch):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    build_calls = []
    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: build_calls.append(a[4]))
    monkeypatch.setattr(sr, "_run_shared_phase1", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_shared_control", lambda *a, **k: {"total_ach_effective": 3.0})
    monkeypatch.setattr(sr, "write_source_topo_set_dict", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_copy_base_case", lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})

    def fake_run_scenario(case_dir, room, settings, z, ach, adv, z_summary, log_fn, should_stop, solver_log_fn,
                           status_fn=None, control_results=None, base_summary=None, should_pause=None):
        return {"reduction_pct": 90.0, "eACH_uv_steady_state": 50.0, "phase1": {"T_ss": 1.0, "live": {"t": [1]}},
                "phase2": {"T_ss": 0.1, "live": {"t": [1]}}}
    monkeypatch.setattr(sr, "_run_scenario", fake_run_scenario)

    removed = []
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, *a, **k: removed.append(cmd))

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"sim-type": "steady_state", "fan-enable": False, "monitoring-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 100, "phase2-iterations": 100, "target-t-ss": 1.0,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3, "z-value": 6,
                "source-zone-size": 0.3}
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1}

    results_seen = []
    sr.run_sweep(
        guv_path="proj.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv=adv,
        z_values=[2, 6], ach_values=[3], log_fn=lambda m: None,
        on_combo_done=lambda z, ach, status, detail: results_seen.append((z, ach, status)),
    )

    assert build_calls == [3]  # one flow base built, for ACH=3
    # Z=2 and Z=6 now run concurrently (shared Z-worker pool - see
    # _run_sweep_concurrent), so completion order isn't guaranteed.
    assert set(results_seen) == {(2, 3, "done"), (6, 3, "done")}
    assert (project_dir / "myproject_Z2_ACH3_report.json").exists()
    assert (project_dir / "myproject_Z6_ACH3_report.json").exists()
    trimmed = json.loads((project_dir / "myproject_Z2_ACH3_report.json").read_text())
    assert "live" not in trimmed["phase1"]
    assert trimmed["reduction_pct"] == 90.0
    assert any("_base_ACH3" in cmd for cmd in removed)  # base dir cleanup happened


def test_run_sweep_captures_build_flow_base_return_value_as_base_summary(tmp_path, monkeypatch):
    # Regression: build_ach_fn used to call _build_flow_base(...) without
    # assigning its return value at all, so ach_delivery/flow_converged
    # never reached any combo's results.json for steady-state sweeps.
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    fake_base_summary = {"flow_converged": True, "ach_delivery": {"measured_ach": 5.9}, "n_lamps": 4}
    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: fake_base_summary)
    monkeypatch.setattr(sr, "_run_shared_phase1", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_shared_control", lambda *a, **k: {"total_ach_effective": 3.0})
    monkeypatch.setattr(sr, "write_source_topo_set_dict", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_copy_base_case", lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, *a, **k: None)

    captured = {}

    def fake_run_scenario(case_dir, room, settings, z, ach, adv, z_summary, log_fn, should_stop, solver_log_fn,
                           status_fn=None, control_results=None, base_summary=None, should_pause=None):
        captured["base_summary"] = base_summary
        return {"reduction_pct": 90.0, "eACH_uv_steady_state": 50.0, "phase1": {"T_ss": 1.0, "live": {"t": [1]}},
                "phase2": {"T_ss": 0.1, "live": {"t": [1]}}}
    monkeypatch.setattr(sr, "_run_scenario", fake_run_scenario)

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"sim-type": "steady_state", "fan-enable": False, "monitoring-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 100, "phase2-iterations": 100, "target-t-ss": 1.0,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3, "z-value": 6,
                "source-zone-size": 0.3}
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1}

    sr.run_sweep(
        guv_path="proj.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv=adv,
        z_values=[6], ach_values=[3], log_fn=lambda m: None,
    )

    assert captured["base_summary"] == fake_base_summary


def test_run_sweep_keeps_shared_dirs_when_setting_enabled(tmp_path, monkeypatch):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_shared_phase1", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_shared_control", lambda *a, **k: {"total_ach_effective": 3.0})
    monkeypatch.setattr(sr, "write_source_topo_set_dict", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_copy_base_case", lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})

    def fake_run_scenario(case_dir, room, settings, z, ach, adv, z_summary, log_fn, should_stop, solver_log_fn,
                           status_fn=None, control_results=None, base_summary=None, should_pause=None):
        return {"reduction_pct": 90.0, "eACH_uv_steady_state": 50.0, "phase1": {"T_ss": 1.0, "live": {"t": [1]}},
                "phase2": {"T_ss": 0.1, "live": {"t": [1]}}}
    monkeypatch.setattr(sr, "_run_scenario", fake_run_scenario)

    removed = []
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, *a, **k: removed.append(cmd))

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"sim-type": "steady_state", "fan-enable": False, "monitoring-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 100, "phase2-iterations": 100, "target-t-ss": 1.0,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3, "z-value": 6,
                "source-zone-size": 0.3}
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1, "keep-shared-scratch-dirs": True}

    sr.run_sweep(
        guv_path="proj.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv=adv,
        z_values=[6], ach_values=[3], log_fn=lambda m: None,
    )

    assert not any("_base_ACH3" in cmd for cmd in removed)  # no cleanup happened


def test_run_decay_sweep_keeps_shared_dirs_when_setting_enabled(tmp_path, monkeypatch):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_shared_control", lambda *a, **k: {"total_ach_effective": 3.0})
    monkeypatch.setattr(sr, "_copy_base_case", lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})
    monkeypatch.setattr(sr, "_run_decay_scenario", lambda *a, **k: {
        "reduction_pct": 1.0, "eACH_uv_effective": 1.0, "eACH_uv_well_mixed": 1.0, "phase1": {}, "phase2": {}})

    removed = []
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, *a, **k: removed.append(cmd))

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"sim-type": "steady_state", "fan-enable": False, "monitoring-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 100, "phase2-iterations": 100, "target-t-ss": 1.0,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3, "z-value": 6,
                "source-zone-size": 0.3}
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1, "keep-shared-scratch-dirs": True}

    sr.run_decay_sweep(
        guv_path="proj.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv=adv,
        z_values=[6], ach_values=[3], log_fn=lambda m: None,
    )

    assert not any("_base_ACH3" in cmd for cmd in removed)  # no cleanup happened


def test_skip_if_combo_already_done_reports_existing_result_without_running():
    calls = []

    def trim_fn(result):
        calls.append(result)
        return {"trimmed": True, **result}

    reported = []
    result = {"reduction_pct": 42.0}
    import tempfile
    with tempfile.TemporaryDirectory() as case_dir:
        with open(f"{case_dir}/results.json", "w") as f:
            json.dump(result, f)
        skipped = sr._skip_if_combo_already_done(
            case_dir, "Z6_ACH6", lambda m: None, trim_fn,
            lambda z, ach, status, detail: reported.append((z, ach, status, detail)), 6, 6)

    assert skipped is True
    assert calls == [result]
    assert reported == [(6, 6, "done", {"trimmed": True, "reduction_pct": 42.0})]


def test_skip_if_combo_already_done_false_when_no_results_json(tmp_path):
    case_dir = tmp_path / "Z6_ACH6"
    case_dir.mkdir()
    reported = []
    skipped = sr._skip_if_combo_already_done(
        str(case_dir), "Z6_ACH6", lambda m: None, lambda r: r,
        lambda *a: reported.append(a), 6, 6)
    assert skipped is False
    assert reported == []


def test_skip_if_combo_already_done_false_and_logs_on_unparseable_json(tmp_path):
    case_dir = tmp_path / "Z6_ACH6"
    case_dir.mkdir()
    (case_dir / "results.json").write_text("not valid json")
    logged = []
    reported = []
    skipped = sr._skip_if_combo_already_done(
        str(case_dir), "Z6_ACH6", logged.append, lambda r: r,
        lambda *a: reported.append(a), 6, 6)
    assert skipped is False
    assert reported == []
    assert any("couldn't be" in line for line in logged)


def test_run_sweep_skips_a_combo_that_already_has_results_json(tmp_path, monkeypatch):
    # Re-launching a sweep at the same project_dir (app restarted, or an
    # earlier attempt was cancelled partway through) should pick up where
    # it left off - a combo that already completed shouldn't be silently
    # redone, only whichever combos are still missing a results.json.
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    # Z=2's combo already finished in an earlier attempt.
    done_dir = project_dir / "Z2_ACH3"
    done_dir.mkdir()
    existing_result = {"reduction_pct": 77.0, "eACH_uv_steady_state": 12.0,
                        "phase1": {"T_ss": 1.0}, "phase2": {"T_ss": 0.1}}
    with open(done_dir / "results.json", "w") as f:
        json.dump(existing_result, f)

    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_shared_phase1", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_shared_control", lambda *a, **k: {"total_ach_effective": 3.0})
    monkeypatch.setattr(sr, "write_source_topo_set_dict", lambda *a, **k: None)
    copy_calls = []
    monkeypatch.setattr(sr, "_copy_base_case",
                         lambda base, target, log_fn: (copy_calls.append(target),
                                                        __import__("os").makedirs(target, exist_ok=True)))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})

    run_scenario_calls = []

    def fake_run_scenario(case_dir, room, settings, z, ach, adv, z_summary, log_fn, should_stop, solver_log_fn,
                           status_fn=None, control_results=None, base_summary=None, should_pause=None):
        run_scenario_calls.append(z)
        return {"reduction_pct": 90.0, "eACH_uv_steady_state": 50.0, "phase1": {"T_ss": 1.0, "live": {"t": [1]}},
                "phase2": {"T_ss": 0.1, "live": {"t": [1]}}}
    monkeypatch.setattr(sr, "_run_scenario", fake_run_scenario)
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, *a, **k: None)

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"sim-type": "steady_state", "fan-enable": False, "monitoring-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 100, "phase2-iterations": 100, "target-t-ss": 1.0,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3, "z-value": 6,
                "source-zone-size": 0.3}
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1}

    results_seen = []
    sr.run_sweep(
        guv_path="proj.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv=adv,
        z_values=[2, 6], ach_values=[3], log_fn=lambda m: None,
        on_combo_done=lambda z, ach, status, detail: results_seen.append((z, ach, status, detail)),
    )

    # Z=2 (already done) was reported "done" with its EXISTING result, and
    # never had _copy_base_case/_run_scenario called for it; Z=6 (not yet
    # done) ran normally.
    assert 2 not in run_scenario_calls
    assert 6 in run_scenario_calls
    assert str(done_dir) not in copy_calls
    seen_by_z = {z: (status, detail) for z, ach, status, detail in results_seen}
    assert seen_by_z[2][0] == "done"
    assert seen_by_z[2][1]["reduction_pct"] == 77.0  # the OLD result, not re-run
    assert seen_by_z[6][0] == "done"
    assert seen_by_z[6][1]["reduction_pct"] == 90.0


def test_run_sweep_skips_failed_combo_and_continues(tmp_path, monkeypatch):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_shared_phase1", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_shared_control", lambda *a, **k: {"total_ach_effective": 3.0})
    monkeypatch.setattr(sr, "write_source_topo_set_dict", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_copy_base_case", lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda *a, **k: None)

    def fake_apply_z(case_dir, z, nbins, fan_kwargs, log_fn):
        if z == 2:
            raise RuntimeError("boom")
        return {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0}
    monkeypatch.setattr(sr, "_apply_z", fake_apply_z)
    monkeypatch.setattr(sr, "_run_scenario", lambda *a, **k: {
        "reduction_pct": 1.0, "eACH_uv_steady_state": 1.0, "phase1": {}, "phase2": {}})

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"sim-type": "steady_state", "fan-enable": False, "monitoring-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 100, "phase2-iterations": 100, "target-t-ss": 1.0,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3, "z-value": 6,
                "source-zone-size": 0.3}

    seen = []
    sr.run_sweep(
        guv_path="p.guv", settings_path="p.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv={"uv-zone-bins": 25, "mesh-cell-size": 0.1},
        z_values=[2, 6], ach_values=[3], log_fn=lambda m: None,
        on_combo_done=lambda z, ach, status, detail: seen.append((z, status)),
    )

    # Z=2 and Z=6 now run concurrently, so completion order isn't
    # guaranteed - only that combo 1 failing didn't stop combo 2.
    assert set(seen) == {(2, "error"), (6, "done")}


def test_run_scenario_threads_control_results_into_measured_ventilation_ach(monkeypatch):
    # _run_scenario is the only place that knows about control_results (the
    # shared, once-per-ACH UV-off control - see _run_shared_control) - it
    # must forward its measured rate into run_steady_state_scenario's
    # measured_ventilation_ach so the corrected eACH_uv/reduction_pct use
    # a real measured ventilation rate instead of Phase 1's own T_ss1 (see
    # compute_corrected_eACH_uv_from_control's docstring).
    calls = []
    def fake_run_steady_state_scenario(*a, **k):
        calls.append(k)
        return {"reduction_pct": 90.0, "eACH_uv_steady_state": 50.0}
    monkeypatch.setattr(sr, "run_steady_state_scenario", fake_run_steady_state_scenario)

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"fan-enable": False, "inlet2-enable": False, "outlet2-enable": False,
                "inlet-wall": "xMin", "inlet-y-input": 1.5, "inlet-z-input": 1.3,
                "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 100, "phase2-iterations": 100, "target-t-ss": 1.0,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3,
                "monitoring-enable": False, "source-zone-size": 0.3}
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1,
           "plateau-rel-tol": 1.0, "t-infinity-early-stop-enabled": False, "keep-all-timesteps": False}

    sr._run_scenario("case_dir", room, settings, z=6.0, ach=3.0, adv=adv,
                      z_summary={"eACH_uv_well_mixed_mean": 0.0, "fluence_mean": 1.0}, log_fn=lambda m: None,
                      should_stop=None, solver_log_fn=None,
                      control_results={"total_ach_effective": 2.46})

    assert calls[-1]["measured_ventilation_ach"] == 2.46


def test_run_scenario_threads_base_summary_into_flow_converged_and_ach_delivery(monkeypatch):
    # Regression: run_sweep's build_ach_fn used to call _build_flow_base()
    # without capturing its return value at all, so steady-state sweep
    # results never had flow_converged/ach_delivery/n_lamps - confirmed on
    # a real sweep (ach_delivery was silently None in every combo's
    # results.json, while the identical decay-mode sweep had it, since
    # _run_decay_scenario already threads base_summary through correctly).
    def fake_run_steady_state_scenario(*a, **k):
        return {"reduction_pct": 90.0, "eACH_uv_steady_state": 50.0}
    monkeypatch.setattr(sr, "run_steady_state_scenario", fake_run_steady_state_scenario)

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"fan-enable": False, "inlet2-enable": False, "outlet2-enable": False,
                "inlet-wall": "xMin", "inlet-y-input": 1.5, "inlet-z-input": 1.3,
                "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 100, "phase2-iterations": 100, "target-t-ss": 1.0,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3,
                "monitoring-enable": False, "source-zone-size": 0.3}
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1,
           "plateau-rel-tol": 1.0, "t-infinity-early-stop-enabled": False, "keep-all-timesteps": False}

    base_summary = {"flow_converged": True, "ach_delivery": {"measured_ach": 5.9, "ratio": 0.98}, "n_lamps": 4}
    result = sr._run_scenario("case_dir", room, settings, z=6.0, ach=3.0, adv=adv,
                              z_summary={"eACH_uv_well_mixed_mean": 0.0, "fluence_mean": 1.0}, log_fn=lambda m: None,
                              should_stop=None, solver_log_fn=None, base_summary=base_summary)

    assert result["flow_converged"] is True
    assert result["ach_delivery"] == {"measured_ach": 5.9, "ratio": 0.98}
    assert result["n_lamps"] == 4


def test_run_scenario_measured_ventilation_ach_none_without_control_results(monkeypatch):
    calls = []
    def fake_run_steady_state_scenario(*a, **k):
        calls.append(k)
        return {"reduction_pct": 90.0, "eACH_uv_steady_state": 50.0}
    monkeypatch.setattr(sr, "run_steady_state_scenario", fake_run_steady_state_scenario)

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"fan-enable": False, "inlet2-enable": False, "outlet2-enable": False,
                "inlet-wall": "xMin", "inlet-y-input": 1.5, "inlet-z-input": 1.3,
                "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 100, "phase2-iterations": 100, "target-t-ss": 1.0,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3,
                "monitoring-enable": False, "source-zone-size": 0.3}
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1,
           "plateau-rel-tol": 1.0, "t-infinity-early-stop-enabled": False, "keep-all-timesteps": False}

    sr._run_scenario("case_dir", room, settings, z=6.0, ach=3.0, adv=adv,
                      z_summary={"eACH_uv_well_mixed_mean": 0.0, "fluence_mean": 1.0}, log_fn=lambda m: None,
                      should_stop=None, solver_log_fn=None)

    assert calls[-1]["measured_ventilation_ach"] is None


def test_run_shared_phase1_clones_base_dir_and_runs_phase1_only(tmp_path, monkeypatch):
    # Regression: Phase 1 ("source only, no UV") depends only on
    # ach/target_T_ss/source geometry, never Z - confirmed directly on a
    # real sweep where its log showed Phase 1 restarting from scratch for
    # every Z sharing an ACH. _run_shared_phase1 runs it once per ACH,
    # in its own directory cloned from the shared flow base, and stops
    # right after Phase 1 (phase1_only=True) so every Z can later clone
    # this directory and skip straight to Phase 2 via
    # run_steady_state_scenario's own checkpoint-detection.
    copy_calls = []
    monkeypatch.setattr(sr, "_copy_base_case", lambda base, target, log_fn: copy_calls.append((base, target)))

    scenario_calls = []
    def fake_run_steady_state_scenario(case_dir, room_x, room_y, room_z, ach, Z, **kwargs):
        scenario_calls.append({"case_dir": case_dir, "ach": ach, "Z": Z, **kwargs})
    monkeypatch.setattr(sr, "run_steady_state_scenario", fake_run_steady_state_scenario)

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"fan-enable": False, "inlet2-enable": False, "outlet2-enable": False,
                "inlet-wall": "xMin", "inlet-y-input": 1.5, "inlet-z-input": 1.3,
                "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 100, "target-t-ss": 1.0, "z-value": 6,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3,
                "t-ss-window-frac": None, "monitoring-enable": False, "source-zone-size": 0.3}
    adv = {"mesh-cell-size": 0.1, "plateau-rel-tol": 1.0,
           "t-infinity-early-stop-enabled": False, "keep-all-timesteps": False}

    sr._run_shared_phase1("base_dir", "phase1_dir", ach=3.0, room=room, settings=settings, adv=adv,
                           log_fn=lambda m: None, should_stop=None, solver_log_fn=None)

    assert copy_calls == [("base_dir", "phase1_dir")]
    assert len(scenario_calls) == 1
    call = scenario_calls[0]
    assert call["case_dir"] == "phase1_dir"
    assert call["ach"] == 3.0
    assert call["Z"] == 6  # placeholder - Phase 1 has no UV, so Z is irrelevant
    assert call["phase1_only"] is True


def test_run_sweep_recarves_source_zone_after_apply_z_wipes_it(tmp_path, monkeypatch):
    # _apply_z's write_cellzones() rewrites cellZones from scratch,
    # wiping the source cellZone _run_shared_phase1 already carved into
    # the shared phase1_dir - run_z_fn must re-carve it (the same reason
    # _apply_z itself already re-carves the fan zone) or Phase 2's source
    # fvOptions entry would reference a cellZone that no longer exists.
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_shared_phase1", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_shared_control", lambda *a, **k: {"total_ach_effective": 3.0})
    monkeypatch.setattr(sr, "_copy_base_case", lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})
    monkeypatch.setattr(sr, "_run_scenario", lambda *a, **k: {
        "reduction_pct": 90.0, "eACH_uv_steady_state": 50.0, "phase1": {}, "phase2": {}})

    topo_calls = []
    monkeypatch.setattr(sr, "write_source_topo_set_dict",
                         lambda case_dir, center, size, cell_size=None: topo_calls.append((case_dir, center, size)))
    wsl_calls = []
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, *a, **k: wsl_calls.append(cmd))

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"sim-type": "steady_state", "fan-enable": False, "monitoring-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 100, "phase2-iterations": 100, "target-t-ss": 1.0,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3, "z-value": 6,
                "source-zone-size": 0.3}
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1}

    sr.run_sweep(
        guv_path="p.guv", settings_path="p.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv=adv,
        z_values=[6], ach_values=[3], log_fn=lambda m: None,
    )

    assert topo_calls == [(f"{project_dir}/Z6_ACH3", (2, 2.5, 1.3), 0.3)]
    assert any("topoSet" in cmd and "sourceTopoSetDict" in cmd for cmd in wsl_calls)


def test_run_sweep_stop_between_combinations_raises_stopped_by_user(tmp_path, monkeypatch):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_shared_phase1", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_shared_control", lambda *a, **k: {"total_ach_effective": 3.0})
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda *a, **k: None)

    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 1  # stop after the first combination check

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"sim-type": "steady_state", "fan-enable": False, "monitoring-enable": False}

    with pytest.raises(StoppedByUser):
        sr.run_sweep(
            guv_path="p.guv", settings_path="p.guvcfd", project_dir=str(project_dir),
            room=room, settings=settings, adv={"uv-zone-bins": 25},
            z_values=[2, 6], ach_values=[3], log_fn=lambda m: None,
            should_stop=should_stop,
        )


def test_run_decay_scenario_rebuilds_fvoptions_from_this_combos_own_kuv(tmp_path, monkeypatch):
    # Regression: _run_decay_scenario used to assume setup_case()'s
    # one-time fvOptions write (from whatever Z the shared ACH-group flow
    # base was built with) was still valid - it wasn't. _apply_z() only
    # rewrites the kUV *field*, deliberately not the fvOptions splice (see
    # its own docstring) - every Z sharing an ACH group silently reused
    # the first Z's actual UV removal rate in the solver regardless,
    # confirmed directly on a real sweep where two different-Z
    # combinations produced byte-identical decay curves.
    case_dir, fluence = _write_synthetic_case(tmp_path, n_cells=8)
    sr._apply_z(case_dir, Z=6.0, nbins=5, fan_kwargs={}, log_fn=lambda m: None)

    # Everything except the fvOptions rebuild is faked out - this test is
    # only about whether write_fvoptions_file gets this combo's own,
    # current kUV-derived entries, not about the actual solve.
    monkeypatch.setattr(sr, "set_control_dict_time", lambda *a, **k: None)
    monkeypatch.setattr(sr, "splice_fv_options_into_control_dict", lambda *a, **k: (None, 1, 1))
    monkeypatch.setattr(sr, "run_wsl_streaming", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": ""})())
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda *a, **k: None)
    monkeypatch.setattr(sr, "write_results_summary", lambda *a, **k: {
        "eACH_uv_effective": 10.0, "eACH_uv_well_mixed": 20.0,
    })

    written = {}
    monkeypatch.setattr(sr, "write_fvoptions_file", lambda cd, entries: written.__setitem__(cd, entries))

    control_results = {"total_ach_effective": 3.0, "total_ach_effective_ci95": None,
                        "fit_se_per_s": None, "fit_n": None}
    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"fan-enable": False, "inlet2-enable": False, "outlet2-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "monitoring-enable": False, "pimple-write-interval": 3}
    adv = {"uv-zone-bins": 5, "pimple-delta-t": 0.5,
           "decay-ach-min-fraction": 90.0, "decay-each-min-fraction": 90.0, "decay-each-max-fraction": 99.9}

    sr._run_decay_scenario(case_dir, room, settings, z=6.0, ach=3.0, adv=adv,
                            z_summary={"eACH_uv_well_mixed_mean": 20.0}, log_fn=lambda m: None,
                            should_stop=None, solver_log_fn=lambda m: None,
                            control_results=control_results)

    entries_z6 = written[case_dir]
    assert len(entries_z6) > 0

    # Now re-carve for a DIFFERENT Z on the same case dir (same as a 2nd
    # combination reusing the same ACH-group's copied case) and confirm
    # the rebuilt fvOptions entries actually change.
    sr._apply_z(case_dir, Z=1.0, nbins=5, fan_kwargs={}, log_fn=lambda m: None)
    sr._run_decay_scenario(case_dir, room, settings, z=1.0, ach=3.0, adv=adv,
                            z_summary={"eACH_uv_well_mixed_mean": 3.3}, log_fn=lambda m: None,
                            should_stop=None, solver_log_fn=lambda m: None,
                            control_results=control_results)
    entries_z1 = written[case_dir]
    assert entries_z1 != entries_z6


def test_run_decay_scenario_status_fn_gets_time_lines_and_clears_on_finish(tmp_path, monkeypatch):
    # status_fn is the overwrite-in-place alternative to the scrolling log
    # for "Time = N" banners (see _throttled_solver_callback) - with
    # several concurrent combinations each printing one every step,
    # appending them all to the log would flood it the same way the raw
    # per-iteration residual dump already doesn't.
    case_dir, _ = _write_synthetic_case(tmp_path, n_cells=8)
    sr._apply_z(case_dir, Z=6.0, nbins=5, fan_kwargs={}, log_fn=lambda m: None)

    monkeypatch.setattr(sr, "write_fvoptions_file", lambda *a, **k: None)
    monkeypatch.setattr(sr, "splice_fv_options_into_control_dict", lambda *a, **k: (None, 1, 1))
    monkeypatch.setattr(sr, "set_control_dict_time", lambda *a, **k: None)
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda *a, **k: None)
    monkeypatch.setattr(sr, "write_results_summary", lambda *a, **k: {
        "eACH_uv_effective": 10.0, "eACH_uv_well_mixed": 20.0,
    })

    def fake_run_wsl_streaming(cmd, cwd, on_line=None, **k):
        on_line("Time = 12.5")
        return type("R", (), {"returncode": 0, "stdout": ""})()
    monkeypatch.setattr(sr, "run_wsl_streaming", fake_run_wsl_streaming)

    status_calls = []
    control_results = {"total_ach_effective": 3.0, "total_ach_effective_ci95": None,
                        "fit_se_per_s": None, "fit_n": None}
    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"fan-enable": False, "inlet2-enable": False, "outlet2-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "monitoring-enable": False, "pimple-write-interval": 3}
    adv = {"uv-zone-bins": 5, "pimple-delta-t": 0.5,
           "decay-ach-min-fraction": 90.0, "decay-each-min-fraction": 90.0, "decay-each-max-fraction": 99.9}

    sr._run_decay_scenario(case_dir, room, settings, z=6.0, ach=3.0, adv=adv,
                            z_summary={"eACH_uv_well_mixed_mean": 20.0}, log_fn=lambda m: None,
                            should_stop=None, solver_log_fn=None,
                            control_results=control_results, status_fn=lambda k, m: status_calls.append((k, m)))

    key = "Z=6.0/ACH=3.0/UV-on"
    # No log_prefix wrapping here - status_key already carries the combo
    # identity, and the caller (app._poll_scenario) renders "[key] value"
    # itself, so double-prefixing here would just duplicate it.
    assert (key, "Time = 12.5") in status_calls
    assert status_calls[-1] == (key, None)  # cleared once the solve finished


def test_run_shared_control_status_fn_gets_time_lines_and_clears_on_finish(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "prepare_ventilation_only_control", lambda *a, **k: None)
    monkeypatch.setattr(sr, "finish_ventilation_only_control", lambda *a, **k: {"total_ach_effective": 3.0})

    def fake_run_wsl_streaming(cmd, cwd, on_line=None, **k):
        on_line("Time = 5")
        return type("R", (), {"returncode": 0, "stdout": ""})()
    monkeypatch.setattr(sr, "run_wsl_streaming", fake_run_wsl_streaming)

    status_calls = []
    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"inlet2-enable": False, "outlet2-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "pimple-write-interval": 3}
    adv = {"pimple-delta-t": 0.5, "decay-ach-min-fraction": 99.9,
           "decay-each-max-fraction": 99.9, "decay-each-min-fraction": 90.0}

    sr._run_shared_control(str(tmp_path / "base"), str(tmp_path / "control"), ach=3.0, room=room,
                            settings=settings, adv=adv, log_fn=lambda m: None,
                            should_stop=None, solver_log_fn=None, status_fn=lambda k, m: status_calls.append((k, m)))

    key = "ACH=3.0/control"
    assert (key, "Time = 5") in status_calls
    assert status_calls[-1] == (key, None)


def test_run_decay_scenario_uses_configured_write_interval_not_duration_over_100(tmp_path, monkeypatch):
    # Regression: write_interval was silently computed as duration // 100
    # regardless of the user's own "Write interval (s)" setting - typing
    # 3 (say) had no effect at all once the run actually started.
    case_dir, _ = _write_synthetic_case(tmp_path, n_cells=8)
    sr._apply_z(case_dir, Z=6.0, nbins=5, fan_kwargs={}, log_fn=lambda m: None)

    monkeypatch.setattr(sr, "write_fvoptions_file", lambda *a, **k: None)
    monkeypatch.setattr(sr, "splice_fv_options_into_control_dict", lambda *a, **k: (None, 1, 1))
    monkeypatch.setattr(sr, "run_wsl_streaming", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": ""})())
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda *a, **k: None)
    monkeypatch.setattr(sr, "write_results_summary", lambda *a, **k: {
        "eACH_uv_effective": 10.0, "eACH_uv_well_mixed": 20.0,
    })

    control_time_calls = []
    monkeypatch.setattr(sr, "set_control_dict_time", lambda case_dir, end_time=None, write_interval=None,
                         delta_t=None: control_time_calls.append(("main", write_interval)))

    control_results = {"total_ach_effective": 3.0, "total_ach_effective_ci95": None,
                        "fit_se_per_s": None, "fit_n": None}
    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"fan-enable": False, "inlet2-enable": False, "outlet2-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "monitoring-enable": False, "pimple-write-interval": 3}
    # A duration long enough that duration // 100 (the old, wrong formula)
    # would clearly differ from the configured 3s.
    adv = {"uv-zone-bins": 5, "pimple-delta-t": 0.5,
           "decay-ach-min-fraction": 99.9, "decay-each-min-fraction": 99.9, "decay-each-max-fraction": 99.9}

    sr._run_decay_scenario(case_dir, room, settings, z=6.0, ach=3.0, adv=adv,
                            z_summary={"eACH_uv_well_mixed_mean": 20.0}, log_fn=lambda m: None,
                            should_stop=None, solver_log_fn=lambda m: None,
                            control_results=control_results)

    assert control_time_calls == [("main", 3)]


def test_run_shared_control_uses_configured_write_interval(tmp_path, monkeypatch):
    # Companion to the above: the UV-off control's write interval is now
    # set inside _run_shared_control (run once per ACH), not inside
    # _run_decay_scenario.
    prepare_calls = []
    monkeypatch.setattr(sr, "prepare_ventilation_only_control", lambda case_dir, control_dir, ach, x, y, z,
                         inlet_wall, inlet_size, end_time, write_interval, **k:
                         prepare_calls.append(("control", write_interval)))
    monkeypatch.setattr(sr, "run_wsl_streaming", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": ""})())
    monkeypatch.setattr(sr, "finish_ventilation_only_control", lambda *a, **k: {"total_ach_effective": 3.0})

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"inlet2-enable": False, "outlet2-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "pimple-write-interval": 3}
    adv = {"pimple-delta-t": 0.5, "decay-ach-min-fraction": 99.9,
           "decay-each-max-fraction": 99.9, "decay-each-min-fraction": 90.0}

    sr._run_shared_control(str(tmp_path / "base"), str(tmp_path / "control"), ach=3.0, room=room,
                            settings=settings, adv=adv, log_fn=lambda m: None,
                            should_stop=None, solver_log_fn=lambda m: None)

    assert prepare_calls == [("control", 3)]


# --- _run_sweep_concurrent (see wsl_utils._kill_wsl_in_dir for the matching
# kill-scoping fix that makes this safe): bounded ACH/Z concurrency shared
# by run_sweep/run_decay_sweep ---

def test_run_sweep_concurrent_bounds_ach_and_z_concurrency(monkeypatch):
    monkeypatch.setattr(sr, "_MAX_CONCURRENT_ACH", 2)
    monkeypatch.setattr(sr, "_MAX_CONCURRENT_Z", 2)

    lock = threading.Lock()
    state = {"ach_live": 0, "ach_peak": 0, "z_live": 0, "z_peak": 0}

    def build_ach_fn(ach):
        with lock:
            state["ach_live"] += 1
            state["ach_peak"] = max(state["ach_peak"], state["ach_live"])
        time.sleep(0.05)
        with lock:
            state["ach_live"] -= 1
        return {"ach": ach}

    def run_z_fn(ctx, z, ach):
        with lock:
            state["z_live"] += 1
            state["z_peak"] = max(state["z_peak"], state["z_live"])
        time.sleep(0.05)
        with lock:
            state["z_live"] -= 1

    cleaned_up = []
    def cleanup_ach_fn(ctx):
        cleaned_up.append(ctx["ach"])

    achs = [1, 2, 3, 4]
    combos = [(z, ach) for ach in achs for z in (10, 20, 30)]  # 3 Z's per ACH, more than either pool size

    sr._run_sweep_concurrent(achs, combos, should_stop=None,
                              build_ach_fn=build_ach_fn, run_z_fn=run_z_fn, cleanup_ach_fn=cleanup_ach_fn)

    assert state["ach_peak"] <= 2
    assert state["z_peak"] <= 2
    assert sorted(cleaned_up) == achs  # every ACH group's cleanup ran exactly once


def test_run_sweep_concurrent_isolates_per_z_errors(monkeypatch):
    monkeypatch.setattr(sr, "_MAX_CONCURRENT_ACH", 3)
    monkeypatch.setattr(sr, "_MAX_CONCURRENT_Z", 3)

    done = []
    lock = threading.Lock()

    def run_z_fn(ctx, z, ach):
        if z == 2:
            # A real run_z_fn (see run_sweep/run_decay_sweep) catches its
            # own Exception and reports "error" instead of propagating -
            # this fake mirrors that contract.
            with lock:
                done.append((z, ach, "error"))
            return
        with lock:
            done.append((z, ach, "done"))

    combos = [(2, 3), (6, 3)]
    sr._run_sweep_concurrent([3], combos, should_stop=None,
                              build_ach_fn=lambda ach: {"ach": ach},
                              run_z_fn=run_z_fn, cleanup_ach_fn=lambda ctx: None)

    assert set(done) == {(2, 3, "error"), (6, 3, "done")}


def test_run_sweep_concurrent_propagates_stopped_by_user():
    def build_ach_fn(ach):
        return {"ach": ach}

    def run_z_fn(ctx, z, ach):
        raise StoppedByUser("stop requested mid-combination")

    with pytest.raises(StoppedByUser):
        sr._run_sweep_concurrent([3], [(2, 3)], should_stop=None,
                                  build_ach_fn=build_ach_fn, run_z_fn=run_z_fn,
                                  cleanup_ach_fn=lambda ctx: None)
