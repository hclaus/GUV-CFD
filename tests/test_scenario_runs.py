import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

import guvcfd.scenario_runs as sr
import guvcfd.steady_state_pipeline as sspl
from guvcfd.case_io import read_openfoam_scalar_field, write_scalar_field
from guvcfd.project_status import load_project_status
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


def test_throttled_solver_callback_appends_total_time_suffix_when_given():
    status_calls = []
    callback = sr._throttled_solver_callback(
        lambda m: None, "prefix", status_fn=lambda k, m: status_calls.append((k, m)), status_key="key",
        total_time=2763)
    callback("Time = 1104")
    assert status_calls == [("key", "Time = 1104 of 2763 total seconds")]


def test_throttled_solver_callback_omits_total_time_suffix_when_not_given():
    status_calls = []
    callback = sr._throttled_solver_callback(
        lambda m: None, "prefix", status_fn=lambda k, m: status_calls.append((k, m)), status_key="key")
    callback("Time = 1104")
    assert status_calls == [("key", "Time = 1104")]


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
    monkeypatch.setattr(sr, "compute_uv_fingerprint", lambda *a, **k: "fake-uv-fp")

    def fake_run_scenario(case_dir, room, settings, z, ach, adv, z_summary, log_fn, should_stop, solver_log_fn,
                           status_fn=None, control_results_future=None, base_summary=None, should_pause=None):
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

    # project_status.json (2026-08-10) - both combos recorded as done, with
    # both fingerprints set.
    status = load_project_status(str(project_dir), "myproject")
    assert status["sim_type"] == "steady_state"
    assert status["guv_path"] == "proj.guv" and status["settings_path"] == "proj.guvcfd"
    for key in ("Z2_ACH3", "Z6_ACH3"):
        combo = status["combos"][key]
        assert combo["status"] == "done"
        assert combo["flow_fingerprint"] and combo["uv_fingerprint"] == "fake-uv-fp"
        assert combo["started_at"] and combo["finished_at"]


def test_run_decay_sweep_records_error_status_in_project_status(tmp_path, monkeypatch):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_shared_control", lambda *a, **k: {"total_ach_effective": 3.0})
    monkeypatch.setattr(sr, "_copy_base_case", lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda *a, **k: None)

    def fake_apply_z(case_dir, z, nbins, fan_kwargs, log_fn):
        raise RuntimeError("boom")
    monkeypatch.setattr(sr, "_apply_z", fake_apply_z)

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"sim-type": "decay", "fan-enable": False, "inlet2-enable": False, "outlet2-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3, "mech-ach-only": False,
                "pimple-write-interval": 3}
    adv = {"uv-zone-bins": 5, "pimple-delta-t": 0.5, "max-co": 5,
           "decay-ach-min-fraction": 90.0, "decay-each-min-fraction": 90.0, "decay-each-max-fraction": 99.9}

    sr.run_decay_sweep(
        guv_path="p.guv", settings_path="p.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv=adv,
        z_values=[6], ach_values=[3], log_fn=lambda m: None,
    )

    status = load_project_status(str(project_dir), "proj")
    combo = status["combos"]["Z6_ACH3"]
    assert combo["status"] == "error"
    assert "boom" in combo["error_message"]
    assert combo["flow_fingerprint"]  # still recorded even though the run failed
    assert combo.get("uv_fingerprint") is None  # never reached that point


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
    monkeypatch.setattr(sr, "compute_uv_fingerprint", lambda *a, **k: "fake-uv-fp")
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, *a, **k: None)

    captured = {}

    def fake_run_scenario(case_dir, room, settings, z, ach, adv, z_summary, log_fn, should_stop, solver_log_fn,
                           status_fn=None, control_results_future=None, base_summary=None, should_pause=None):
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


def test_run_sweep_passes_base_summary_to_run_shared_control(tmp_path, monkeypatch):
    # Regression: run_sweep's build_ach_fn called _run_shared_control(...)
    # without base_summary at all (a required positional arg added for the
    # inlet-velocity-area fix - see ventilation_control.
    # prepare_ventilation_only_control's docstring) - only run_decay_sweep's
    # own call site got updated at the time, so a real steady-state sweep
    # crashed with "missing 1 required positional argument: 'base_summary'"
    # the first time a user actually ran one after that fix. A strict
    # (non-**k-catchall) lambda here mirrors _run_shared_control's own
    # positional signature, so a call-site/definition mismatch like this
    # raises immediately instead of being silently swallowed by a permissive
    # mock, the way every other test of this call site does.
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    fake_base_summary = {"flow_converged": True, "ach_delivery": None, "n_lamps": 4}
    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: fake_base_summary)
    monkeypatch.setattr(sr, "_run_shared_phase1", lambda *a, **k: None)

    captured = {}

    def strict_run_shared_control(base_dir, control_dir, ach, room, settings, adv, log_fn, should_stop,
                                   solver_log_fn, base_summary, status_fn=None, should_pause=None, sealed=False):
        captured["base_summary"] = base_summary
        return {"total_ach_effective": 3.0}
    monkeypatch.setattr(sr, "_run_shared_control", strict_run_shared_control)

    monkeypatch.setattr(sr, "write_source_topo_set_dict", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_copy_base_case", lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, *a, **k: None)
    monkeypatch.setattr(sr, "_run_scenario", lambda *a, **k: {
        "reduction_pct": 90.0, "eACH_uv_steady_state": 50.0, "phase1": {"T_ss": 1.0, "live": {"t": [1]}},
        "phase2": {"T_ss": 0.1, "live": {"t": [1]}}})

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
                           status_fn=None, control_results_future=None, base_summary=None, should_pause=None):
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


def _decay_reuse_settings():
    # Every FLOW_FINGERPRINT_FIELDS key populated explicitly (not left for
    # capture_openfoam_settings to backfill mid-sweep) - a real project's
    # settings dict already has all of these from the GUI form/.guvcfd
    # file, and leaving any absent here would let run_z_fn's own
    # capture_openfoam_settings() call silently mutate `settings` with a
    # real default partway through the FIRST sweep, changing what
    # compute_flow_fingerprint sees on a SECOND call even though nothing
    # the user controls actually changed - confirmed as the root cause of
    # a spurious fingerprint mismatch while writing these tests.
    return {
        "sim-type": "decay", "mech-ach-only": False, "monitoring-enable": False,
        "inlet-wall": "xMin", "inlet-y-input": 2.5, "inlet-z-input": 0.4,
        "inlet-size-w": 0.3, "inlet-size-h": 0.3, "inlet-diffuser-type": "direct",
        "inlet2-enable": False, "inlet2-wall": "ceiling", "inlet2-y-input": 1.5, "inlet2-z-input": 1.5,
        "inlet2-size-w": 0.3, "inlet2-size-h": 0.3, "inlet2-diffuser-type": "direct",
        "outlet-wall": "xMax", "outlet-y-input": 2.5, "outlet-z-input": 2.7,
        "outlet-size-w": 0.3, "outlet-size-h": 0.3,
        "outlet2-enable": False, "outlet2-wall": "floor", "outlet2-y-input": 1.5, "outlet2-z-input": 1.5,
        "outlet2-size-w": 0.3, "outlet2-size-h": 0.3,
        "mesh-cell-size": 0.1, "momentum-relaxation": 0.7, "scalar-relaxation": 0.7,
        "fan-enable": False, "fan-speed": 0.4, "fan-direction": "down", "fan-radius": 0.45,
        "fan-thickness": 0.2, "fan-x-input": 2.0, "fan-y-input": 1.5, "fan-z-input": 2.2,
    }


# --- _find_done_combo_case_dir_for_ach / _seed_ach_base_from_existing_combo /
# _seed_ach_base_if_no_scratch_survives (2026-08-10 incident fix) ---

def test_find_done_combo_case_dir_for_ach_finds_matching_done_combo(tmp_path):
    from guvcfd.project_status import update_combo_status
    update_combo_status(str(tmp_path), "myproj", z=1.0, ach=3.0, status="done", flow_fingerprint="fp1",
                         sim_type="steady_state")
    donor = sr._find_done_combo_case_dir_for_ach(str(tmp_path), "myproj", 3.0, "fp1", "steady_state", "steady_state")
    assert donor == f"{tmp_path}/Z1_ACH3"


def test_find_done_combo_case_dir_for_ach_uses_recorded_subdir_not_a_recompute(tmp_path):
    # A combo belonging to a non-original design has a guv-suffixed
    # subdir that recomputing _subdir_name(z, ach) alone would miss (see
    # compute_guv_design_suffix's own docstring) - the recorded "subdir"
    # field must be used verbatim, not reconstructed.
    from guvcfd.project_status import update_combo_status
    update_combo_status(str(tmp_path), "myproj", z=1.0, ach=3.0, status="done", flow_fingerprint="fp1",
                         guv_path="lampB.guv", combo_suffix="_lampB", subdir="Z1_ACH3_lampB",
                         sim_type="steady_state")
    donor = sr._find_done_combo_case_dir_for_ach(str(tmp_path), "myproj", 3.0, "fp1", "steady_state", "steady_state")
    assert donor == f"{tmp_path}/Z1_ACH3_lampB"


def test_find_done_combo_case_dir_for_ach_none_when_fingerprint_mismatches(tmp_path):
    from guvcfd.project_status import update_combo_status
    update_combo_status(str(tmp_path), "myproj", z=1.0, ach=3.0, status="done", flow_fingerprint="fp1",
                         sim_type="steady_state")
    assert sr._find_done_combo_case_dir_for_ach(
        str(tmp_path), "myproj", 3.0, "fp2", "steady_state", "steady_state") is None


def test_find_done_combo_case_dir_for_ach_none_when_status_not_done(tmp_path):
    from guvcfd.project_status import update_combo_status
    update_combo_status(str(tmp_path), "myproj", z=1.0, ach=3.0, status="running", flow_fingerprint="fp1",
                         sim_type="steady_state")
    assert sr._find_done_combo_case_dir_for_ach(
        str(tmp_path), "myproj", 3.0, "fp1", "steady_state", "steady_state") is None


def test_find_done_combo_case_dir_for_ach_none_when_wrong_ach(tmp_path):
    from guvcfd.project_status import update_combo_status
    update_combo_status(str(tmp_path), "myproj", z=1.0, ach=6.0, status="done", flow_fingerprint="fp1",
                         sim_type="steady_state")
    assert sr._find_done_combo_case_dir_for_ach(
        str(tmp_path), "myproj", 3.0, "fp1", "steady_state", "steady_state") is None


# --- _find_done_combo_case_dir_for_ach's sim_type-matching safety gate
# (2026-08-12): a steady-state combo's case_dir is a copy of Phase 1's own
# case, which can carry a non-zero warm-started T field that stripping
# doesn't remove - unsafe to seed a Decay-mode base from. ---

def test_find_done_combo_case_dir_for_ach_rejects_a_cross_mode_donor(tmp_path):
    from guvcfd.project_status import update_combo_status
    update_combo_status(str(tmp_path), "myproj", z=1.0, ach=3.0, status="done", flow_fingerprint="fp1",
                         sim_type="steady_state")
    # Looking for a DECAY-mode donor - the only recorded combo is
    # steady-state, so it must not be offered as a donor.
    assert sr._find_done_combo_case_dir_for_ach(
        str(tmp_path), "myproj", 3.0, "fp1", "decay", "steady_state") is None


def test_find_done_combo_case_dir_for_ach_accepts_a_same_mode_donor(tmp_path):
    from guvcfd.project_status import update_combo_status
    update_combo_status(str(tmp_path), "myproj", z=1.0, ach=3.0, status="done", flow_fingerprint="fp1",
                         sim_type="decay")
    donor = sr._find_done_combo_case_dir_for_ach(str(tmp_path), "myproj", 3.0, "fp1", "decay", "steady_state")
    assert donor == f"{tmp_path}/Z1_ACH3"


def test_find_done_combo_case_dir_for_ach_falls_back_to_original_sim_type_for_old_combos(tmp_path):
    # A combo written before per-combo sim_type tracking existed has no
    # "sim_type" of its own - original_sim_type (the project's own
    # first-ever recorded mode) is the correct assumption for it, since
    # mode-switching didn't exist yet when it was written.
    from guvcfd.project_status import update_combo_status
    update_combo_status(str(tmp_path), "myproj", z=1.0, ach=3.0, status="done", flow_fingerprint="fp1")
    donor = sr._find_done_combo_case_dir_for_ach(
        str(tmp_path), "myproj", 3.0, "fp1", "steady_state", "steady_state")
    assert donor == f"{tmp_path}/Z1_ACH3"
    # But NOT if the current sweep is a genuine mode switch - an old,
    # sim_type-less combo can't be trusted as a decay donor just because
    # the project's original mode happened to be something else.
    assert sr._find_done_combo_case_dir_for_ach(
        str(tmp_path), "myproj", 3.0, "fp1", "decay", "steady_state") is None


def test_seed_ach_base_from_existing_combo_copies_and_strips(monkeypatch):
    captured = {}
    monkeypatch.setattr(sr, "wsl_path", lambda p: p)
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, cwd, desc: captured.update(cmd=cmd))

    sr._seed_ach_base_from_existing_combo("/proj/Z1_ACH3", "/proj/_base_ACH3", lambda m: None)

    cmd = captured["cmd"]
    assert 'rm -rf "/proj/_base_ACH3"' in cmd
    assert 'cp -r "/proj/Z1_ACH3" "/proj/_base_ACH3"' in cmd
    assert "rm -rf postProcessing" in cmd and '"0/kUV"' in cmd
    assert "find ." in cmd  # strips stale solved time-directories


def test_seed_ach_base_if_no_scratch_survives_noop_when_fluencerate_present(tmp_path, monkeypatch):
    base_dir = tmp_path / "_base_ACH3"
    (base_dir / "0").mkdir(parents=True)
    (base_dir / "0" / "fluenceRate").write_text("x")

    called = []
    monkeypatch.setattr(sr, "_find_done_combo_case_dir_for_ach", lambda *a, **k: called.append(1))

    sr._seed_ach_base_if_no_scratch_survives(str(tmp_path), "myproj", 3.0, "fp1", str(base_dir), lambda m: None,
                                              "steady_state", "steady_state")
    assert called == []  # never even looked for a donor - nothing to do


def test_seed_ach_base_if_no_scratch_survives_noop_when_no_donor(tmp_path, monkeypatch):
    base_dir = tmp_path / "_base_ACH3"
    monkeypatch.setattr(sr, "_find_done_combo_case_dir_for_ach", lambda *a, **k: None)
    seeded = []
    monkeypatch.setattr(sr, "_seed_ach_base_from_existing_combo", lambda *a, **k: seeded.append(1))

    sr._seed_ach_base_if_no_scratch_survives(str(tmp_path), "myproj", 3.0, "fp1", str(base_dir), lambda m: None,
                                              "steady_state", "steady_state")
    assert seeded == []


def test_seed_ach_base_if_no_scratch_survives_seeds_when_donor_found(tmp_path, monkeypatch):
    base_dir = tmp_path / "_base_ACH3"
    monkeypatch.setattr(sr, "_find_done_combo_case_dir_for_ach", lambda *a, **k: f"{tmp_path}/Z1_ACH3")
    seeded = []
    monkeypatch.setattr(sr, "_seed_ach_base_from_existing_combo",
                         lambda source, base, log_fn: seeded.append((source, base)))

    sr._seed_ach_base_if_no_scratch_survives(str(tmp_path), "myproj", 3.0, "fp1", str(base_dir), lambda m: None,
                                              "steady_state", "steady_state")
    assert seeded == [(f"{tmp_path}/Z1_ACH3", str(base_dir))]


def test_run_decay_sweep_seeds_flow_base_from_existing_done_combo(tmp_path, monkeypatch):
    # Integration guard for the 2026-08-10 incident: adding a NEW Z at an
    # ACH that already has a done combo (but no ach_bases record and no
    # surviving scratch dir - exactly a pre-existing project's situation)
    # must seed from that done combo BEFORE _build_flow_base runs, not
    # pay for a full re-mesh from scratch.
    project_dir = tmp_path
    from guvcfd.project_status import update_combo_status
    settings = _decay_reuse_settings()
    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    fp = __import__("guvcfd.project_status", fromlist=["compute_flow_fingerprint"]).compute_flow_fingerprint(
        settings, room)
    update_combo_status(str(project_dir), "myproject", z=1.0, ach=3.0, status="done", flow_fingerprint=fp)

    seed_calls = []
    monkeypatch.setattr(sr, "_seed_ach_base_if_no_scratch_survives",
                         lambda *a, **k: seed_calls.append(a))
    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: {"flow_converged": True})
    monkeypatch.setattr(sr, "_run_shared_control", lambda *a, **k: {"total_ach_effective": 3.0})
    monkeypatch.setattr(sr, "_copy_base_case",
                         lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})
    monkeypatch.setattr(sr, "compute_uv_fingerprint", lambda *a, **k: "fake-uv-fp")
    monkeypatch.setattr(sr, "_run_decay_scenario", lambda *a, **k: {
        "reduction_pct": 1.0, "eACH_uv_effective": 1.0, "eACH_uv_well_mixed": 1.0, "phase1": {}, "phase2": {}})
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda *a, **k: None)
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1, "keep-shared-scratch-dirs": True}

    sr.run_decay_sweep(
        guv_path="proj.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv=adv,
        z_values=[7], ach_values=[3], log_fn=lambda m: None,
    )

    assert len(seed_calls) == 1
    ach_arg = seed_calls[0][2]
    assert ach_arg == 3


def test_run_decay_sweep_second_launch_reuses_matching_flow_and_control(tmp_path, monkeypatch):
    # Regression guard for the 2026-08-10 fingerprint-gated reuse work: a
    # SECOND run_decay_sweep() call on the same project_dir, with the same
    # flow-affecting settings, must not rerun the shared UV-off control
    # decay for an ACH it already has a valid (fingerprint-matching)
    # record for - only the flow-base build (already had its own,
    # unrelated file-presence reuse check) may still be "called" per this
    # test's mocking, but the expensive control pimpleFoam run must not be.
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    build_calls = []

    def fake_build_flow_base(guv_path, base_dir, room, settings, ach, adv, log_fn, *a, **k):
        build_calls.append(ach)
        __import__("os").makedirs(base_dir, exist_ok=True)
        return {"flow_converged": True, "inlet_velocity": 1.0, "inlet2_velocity": None}

    control_calls = []

    def fake_run_shared_control(base_dir, control_dir, ach, *a, **k):
        control_calls.append(ach)
        __import__("os").makedirs(control_dir, exist_ok=True)
        return {"total_ach_effective": 3.0}

    monkeypatch.setattr(sr, "_build_flow_base", fake_build_flow_base)
    monkeypatch.setattr(sr, "_run_shared_control", fake_run_shared_control)
    monkeypatch.setattr(sr, "_copy_base_case",
                         lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})
    monkeypatch.setattr(sr, "compute_uv_fingerprint", lambda *a, **k: "fake-uv-fp")
    monkeypatch.setattr(sr, "_run_decay_scenario", lambda *a, **k: {
        "reduction_pct": 1.0, "eACH_uv_effective": 1.0, "eACH_uv_well_mixed": 1.0, "phase1": {}, "phase2": {}})
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, *a, **k: None)

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = _decay_reuse_settings()
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1, "keep-shared-scratch-dirs": True}

    sr.run_decay_sweep(
        guv_path="proj.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv=adv,
        z_values=[6], ach_values=[3], log_fn=lambda m: None,
    )
    sr.run_decay_sweep(
        guv_path="proj.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv=adv,
        z_values=[2], ach_values=[3], log_fn=lambda m: None,
    )

    assert control_calls == [3]  # only the FIRST launch's control run - second reused it
    assert (project_dir / "myproject_Z6_ACH3_report.json").exists()
    assert (project_dir / "myproject_Z2_ACH3_report.json").exists()


def test_run_decay_sweep_different_guv_at_same_z_ach_gets_its_own_folder(tmp_path, monkeypatch):
    # Regression guard for the 2026-08-12 incident: applying a genuinely
    # different .guv file to Z/ACH values a project already used - the
    # SAME (z, ach) as an existing "done" combo - used to land on that
    # SAME combo and get silently skipped as "already done" (no new
    # folder, no new report, no error - just the original design's stale
    # numbers reused under the new design's name). The second design must
    # get its own folder/report, and the first design's own files must be
    # completely untouched (byte-identical) by the second launch.
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    def fake_build_flow_base(guv_path, base_dir, room, settings, ach, adv, log_fn, *a, **k):
        __import__("os").makedirs(base_dir, exist_ok=True)
        return {"flow_converged": True, "inlet_velocity": 1.0, "inlet2_velocity": None}

    def fake_run_shared_control(base_dir, control_dir, ach, *a, **k):
        __import__("os").makedirs(control_dir, exist_ok=True)
        return {"total_ach_effective": 3.0}

    monkeypatch.setattr(sr, "_build_flow_base", fake_build_flow_base)
    monkeypatch.setattr(sr, "_run_shared_control", fake_run_shared_control)
    monkeypatch.setattr(sr, "_copy_base_case",
                         lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})
    monkeypatch.setattr(sr, "compute_uv_fingerprint", lambda *a, **k: "fake-uv-fp")
    decay_calls = []
    monkeypatch.setattr(sr, "_run_decay_scenario", lambda *a, **k: decay_calls.append(1) or {
        "reduction_pct": 1.0, "eACH_uv_effective": 1.0, "eACH_uv_well_mixed": 1.0, "phase1": {}, "phase2": {}})
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, *a, **k: None)

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = _decay_reuse_settings()
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1, "keep-shared-scratch-dirs": True}

    results_seen = []
    sr.run_decay_sweep(
        guv_path="lampA.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv=adv,
        z_values=[6], ach_values=[3], log_fn=lambda m: None,
        on_combo_done=lambda z, ach, status, detail: results_seen.append((z, ach, status)),
    )
    original_report = (project_dir / "myproject_Z6_ACH3_report.json").read_text()

    sr.run_decay_sweep(
        guv_path="lampB.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv=adv,
        z_values=[6], ach_values=[3], log_fn=lambda m: None,
        on_combo_done=lambda z, ach, status, detail: results_seen.append((z, ach, status)),
    )

    # The whole point: the second design's combo actually ran (not
    # skipped as "already done") - 2 real solves, not 1.
    assert len(decay_calls) == 2
    assert results_seen == [(6, 3, "done"), (6, 3, "done")]

    # A genuinely NEW folder/report for the second design...
    assert (project_dir / "Z6_ACH3_lampB").exists()
    assert (project_dir / "myproject_Z6_ACH3_lampB_report.json").exists()
    # ...and the ORIGINAL design's own folder/report are untouched.
    assert (project_dir / "myproject_Z6_ACH3_report.json").read_text() == original_report

    status = load_project_status(str(project_dir), "myproject")
    assert set(status["combos"]) == {"Z6_ACH3", "Z6_ACH3_lampB"}
    assert status["combos"]["Z6_ACH3"]["guv_path"] == "lampA.guv"
    assert status["combos"]["Z6_ACH3_lampB"]["guv_path"] == "lampB.guv"
    assert status["guv_path"] == "lampA.guv"  # the project's original design, never overwritten


def test_switching_a_steady_state_project_to_decay_mode_gets_its_own_folder_and_reuses_flow(tmp_path, monkeypatch):
    # Regression guard for the 2026-08-12 mode-switch feature: re-
    # evaluating an already-swept Z/ACH under Decay mode instead of
    # Steady-state must NOT land on and get skipped as that same combo -
    # it needs its own folder, and (per the whole point of the feature)
    # should reuse the already-converged flow field/UV-off control run,
    # only running each Z's own new UV-on decay solve.
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    build_calls = []

    def fake_build_flow_base(guv_path, base_dir, room, settings, ach, adv, log_fn, *a, **k):
        build_calls.append(ach)
        __import__("os").makedirs(f"{base_dir}/0", exist_ok=True)
        return {"flow_converged": True, "inlet_velocity": 1.0, "inlet2_velocity": None}

    control_calls = []

    def fake_run_shared_control(base_dir, control_dir, ach, *a, **k):
        control_calls.append(ach)
        __import__("os").makedirs(control_dir, exist_ok=True)
        return {"total_ach_effective": 3.0}

    monkeypatch.setattr(sr, "_build_flow_base", fake_build_flow_base)
    monkeypatch.setattr(sr, "_run_shared_phase1", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_shared_control", fake_run_shared_control)
    monkeypatch.setattr(sr, "write_source_topo_set_dict", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_copy_base_case",
                         lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})
    monkeypatch.setattr(sr, "compute_uv_fingerprint", lambda *a, **k: "fake-uv-fp")
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, *a, **k: None)
    monkeypatch.setattr(sr, "_run_scenario", lambda *a, **k: {
        "reduction_pct": 90.0, "eACH_uv_steady_state": 50.0, "phase1": {"T_ss": 1.0, "live": {"t": [1]}},
        "phase2": {"T_ss": 0.1, "live": {"t": [1]}}})
    decay_calls = []
    monkeypatch.setattr(sr, "_run_decay_scenario", lambda *a, **k: decay_calls.append(1) or {
        "reduction_pct": 1.0, "eACH_uv_effective": 1.0, "eACH_uv_well_mixed": 1.0, "phase1": {}, "phase2": {}})

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    # Every FLOW_FINGERPRINT_FIELDS key populated explicitly (see
    # _decay_reuse_settings's own docstring) - otherwise run_z_fn's own
    # capture_openfoam_settings() call would silently backfill missing
    # fields with real defaults partway through the FIRST (steady-state)
    # sweep, mutating this same dict object such that the SECOND (decay)
    # sweep's own flow_fingerprint no longer matches what got recorded -
    # a spurious mismatch that has nothing to do with the actual feature
    # under test here.
    steady_settings = dict(_decay_reuse_settings(), **{
        "sim-type": "steady_state", "monitoring-enable": False,
        "phase1-iterations": 100, "phase2-iterations": 100, "target-t-ss": 1.0,
        "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3, "z-value": 6,
        "source-zone-size": 0.3,
    })
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1, "keep-shared-scratch-dirs": True,
           "pimple-delta-t": 0.5, "max-co": 5,
           "decay-ach-min-fraction": 90.0, "decay-each-min-fraction": 90.0, "decay-each-max-fraction": 99.9}

    sr.run_sweep(
        guv_path="room.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=steady_settings, adv=adv,
        z_values=[6], ach_values=[3], log_fn=lambda m: None,
    )
    original_report = (project_dir / "myproject_Z6_ACH3_report.json").read_text()

    decay_settings = dict(_decay_reuse_settings(), **{
        "monitoring-enable": False, "pimple-write-interval": 3, "z-value": 6,
    })
    results_seen = []
    sr.run_decay_sweep(
        guv_path="room.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=decay_settings, adv=adv,
        z_values=[6], ach_values=[3], log_fn=lambda m: None,
        on_combo_done=lambda z, ach, status, detail: results_seen.append((z, ach, status)),
    )

    # The decay combo actually ran (not skipped as "already done")...
    assert len(decay_calls) == 1
    assert results_seen == [(6, 3, "done")]
    # ...into its own, genuinely new folder/report...
    assert (project_dir / "Z6_ACH3_decay").exists()
    assert (project_dir / "myproject_Z6_ACH3_decay_report.json").exists()
    # ...while the original steady-state combo's own files are untouched.
    assert (project_dir / "myproject_Z6_ACH3_report.json").read_text() == original_report

    # The whole point of the feature: control was measured ONCE (by the
    # steady-state sweep) and REUSED by the decay sweep, not re-run -
    # _prepare_control skips _run_shared_control entirely once
    # find_reusable_ach_base matches, which this genuinely mode-agnostic
    # reuse path (ach_bases record + a surviving scratch dir) does
    # regardless of sim_type - only the seed-from-existing-combo fallback
    # (irrelevant here, since the real scratch dir survives) is
    # restricted to same-mode donors (see _find_done_combo_case_dir_for_ach's
    # own docstring). _build_flow_base itself is still called once per
    # sweep launch either way (real reuse-vs-fresh-build is decided
    # INSIDE it - out of scope for this mock, which stands in for both).
    assert build_calls == [3, 3]
    assert control_calls == [3]

    status = load_project_status(str(project_dir), "myproject")
    assert set(status["combos"]) == {"Z6_ACH3", "Z6_ACH3_decay"}
    assert status["combos"]["Z6_ACH3"]["sim_type"] == "steady_state"
    assert status["combos"]["Z6_ACH3_decay"]["sim_type"] == "decay"
    assert status["sim_type"] == "steady_state"  # the project's original, never overwritten


def test_run_decay_sweep_rebuilds_when_flow_settings_change_between_launches(tmp_path, monkeypatch):
    # Companion to the reuse test above: a changed flow-affecting setting
    # between two launches must be detected as a fingerprint MISMATCH, not
    # silently reused - the stale scratch dirs get discarded and the
    # control run reruns.
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    control_calls = []

    def fake_build_flow_base(guv_path, base_dir, room, settings, ach, adv, log_fn, *a, **k):
        __import__("os").makedirs(base_dir, exist_ok=True)
        return {"flow_converged": True, "inlet_velocity": 1.0, "inlet2_velocity": None}

    def fake_run_shared_control(base_dir, control_dir, ach, *a, **k):
        control_calls.append(ach)
        __import__("os").makedirs(control_dir, exist_ok=True)
        return {"total_ach_effective": 3.0}

    removed = []
    monkeypatch.setattr(sr, "_build_flow_base", fake_build_flow_base)
    monkeypatch.setattr(sr, "_run_shared_control", fake_run_shared_control)
    monkeypatch.setattr(sr, "_copy_base_case",
                         lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})
    monkeypatch.setattr(sr, "compute_uv_fingerprint", lambda *a, **k: "fake-uv-fp")
    monkeypatch.setattr(sr, "_run_decay_scenario", lambda *a, **k: {
        "reduction_pct": 1.0, "eACH_uv_effective": 1.0, "eACH_uv_well_mixed": 1.0, "phase1": {}, "phase2": {}})
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, *a, **k: removed.append(cmd))

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    base_settings = _decay_reuse_settings()
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1, "keep-shared-scratch-dirs": True}

    sr.run_decay_sweep(
        guv_path="proj.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=dict(base_settings), adv=adv,
        z_values=[6], ach_values=[3], log_fn=lambda m: None,
    )
    changed_settings = dict(base_settings, **{"inlet-size-w": 0.5})  # flow-affecting change
    sr.run_decay_sweep(
        guv_path="proj.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=changed_settings, adv=adv,
        z_values=[2], ach_values=[3], log_fn=lambda m: None,
    )

    assert control_calls == [3, 3]  # reran, since the flow settings changed
    assert any("_base_ACH3" in cmd for cmd in removed)  # stale scratch discarded


def test_run_decay_sweep_mechanical_ach_only_skips_apply_z_and_reuses_control_results(tmp_path, monkeypatch):
    # No UV at all with mechanical_ach_only - Z is physically irrelevant
    # (nothing in the case depends on it), so run_z_fn must never call
    # _apply_z/_run_decay_scenario (which read 0/kUV, absent here) and
    # should instead just reuse the shared per-ACH control run's own
    # measurement for every Z sharing that ACH.
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: {"flow_converged": True})

    control_result = {"eACH_uv_effective": 0.0, "eACH_uv_well_mixed": 0.0, "total_ach_effective": 3.2}

    def fake_run_shared_control(base_dir, control_dir, ach, *a, **k):
        __import__("os").makedirs(control_dir, exist_ok=True)
        sr.write_case_file(control_dir, "results.json", json.dumps(control_result))
        return control_result

    monkeypatch.setattr(sr, "_run_shared_control", fake_run_shared_control)
    monkeypatch.setattr(sr, "_copy_base_case",
                         lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))

    def fail(*a, **k):
        raise AssertionError("must not touch the UV pipeline for mechanical_ach_only")

    monkeypatch.setattr(sr, "_apply_z", fail)
    monkeypatch.setattr(sr, "_run_decay_scenario", fail)

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"sim-type": "decay", "mech-ach-only": True, "fan-enable": False,
                "inlet2-enable": False, "outlet2-enable": False, "monitoring-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3}
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1, "keep-shared-scratch-dirs": True}

    sr.run_decay_sweep(
        guv_path="proj.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv=adv,
        z_values=[6], ach_values=[3], log_fn=lambda m: None,
    )

    case_dir = project_dir / "Z6_ACH3"
    assert json.loads((case_dir / "results.json").read_text()) == control_result


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


# --- find_first_guv_path_on_disk / rebuild_project_status_from_disk (2026-08-10) ---

def _write_run_settings(case_dir, **overrides):
    case_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "sim-type": "decay", "guv_path": "proj.guv", "settings_path": "proj.guvcfd",
        "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
    }
    data.update(overrides)
    (case_dir / "run_settings.json").write_text(json.dumps(data))


def test_find_first_guv_path_on_disk_returns_none_when_nothing_there(tmp_path):
    assert sr.find_first_guv_path_on_disk(str(tmp_path)) is None


def test_find_first_guv_path_on_disk_finds_it_in_a_combo_subfolder(tmp_path):
    _write_run_settings(tmp_path / "Z6_ACH3", guv_path="found.guv")
    assert sr.find_first_guv_path_on_disk(str(tmp_path)) == "found.guv"


def test_rebuild_project_status_from_disk_returns_zero_when_nothing_found(tmp_path):
    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    assert sr.rebuild_project_status_from_disk(str(tmp_path), "myproj", room) == 0


def test_rebuild_project_status_from_disk_reconstructs_done_and_incomplete_combos(tmp_path):
    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    done_dir = tmp_path / "Z6_ACH3"
    _write_run_settings(done_dir, **{"z-value": 6.0, "ach": 3.0})
    (done_dir / "results.json").write_text("{}")

    incomplete_dir = tmp_path / "Z2_ACH3"
    _write_run_settings(incomplete_dir, **{"z-value": 2.0, "ach": 3.0})
    # No results.json here - setup started but the combo never finished.

    n_found = sr.rebuild_project_status_from_disk(str(tmp_path), "myproj", room)
    assert n_found == 2

    from guvcfd.project_status import load_project_status
    status = load_project_status(str(tmp_path), "myproj")
    assert status["combos"]["Z6_ACH3"]["status"] == "done"
    assert status["combos"]["Z6_ACH3"]["flow_fingerprint"]
    assert status["combos"]["Z2_ACH3"]["status"] == "incomplete"


def test_rebuild_project_status_from_disk_skips_subfolders_missing_z_or_ach(tmp_path):
    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    bad_dir = tmp_path / "Z_weird"
    _write_run_settings(bad_dir)  # no "z-value"/"ach" keys at all
    assert sr.rebuild_project_status_from_disk(str(tmp_path), "myproj", room) == 0


def test_rebuild_project_status_from_disk_skips_unreadable_run_settings(tmp_path):
    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    bad_dir = tmp_path / "Z6_ACH3"
    bad_dir.mkdir()
    (bad_dir / "run_settings.json").write_text("not valid json")
    assert sr.rebuild_project_status_from_disk(str(tmp_path), "myproj", room) == 0


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
    monkeypatch.setattr(sr, "compute_uv_fingerprint", lambda *a, **k: "fake-uv-fp")

    run_scenario_calls = []

    def fake_run_scenario(case_dir, room, settings, z, ach, adv, z_summary, log_fn, should_stop, solver_log_fn,
                           status_fn=None, control_results_future=None, base_summary=None, should_pause=None):
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
    monkeypatch.setattr(sr, "compute_uv_fingerprint", lambda *a, **k: "fake-uv-fp")
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
           "plateau-rel-tol": 1.0, "t-infinity-early-stop-enabled": False, "phase1-require-stable-extrapolation": False, "keep-all-timesteps": False,
           "deltat-scaling-enabled": False, "deltat-effective-fraction": 0.7, "deltat-target-fraction": 0.995,
           "phase-chunk-size": 400, "phase-write-interval": 200}

    sr._run_scenario("case_dir", room, settings, z=6.0, ach=3.0, adv=adv,
                      z_summary={"eACH_uv_well_mixed_mean": 0.0, "fluence_mean": 1.0}, log_fn=lambda m: None,
                      should_stop=None, solver_log_fn=None,
                      control_results_future=sr._completed_future({"total_ach_effective": 2.46}))

    # The future itself is handed through, not resolved by _run_scenario -
    # see run_steady_state_scenario's own control_results_future docstring
    # for why (must resolve only after its own simpleFoam call, not before).
    assert calls[-1]["control_results_future"].result()["total_ach_effective"] == 2.46


def test_run_scenario_deltat_scaling_uses_configured_iterations_not_settling_inflation(monkeypatch):
    # Regression: _settling_iterations-based inflation and deltaT scaling
    # solve the same equation for opposite unknowns - confirmed directly on
    # a real ACH=6 run that composing them (inflate iterations first, THEN
    # try to scale deltaT against the inflated count) silently discarded
    # the user's actual configured 1500-iteration budget by inflating it to
    # ~3179 (_settling_iterations(6) alone), leaving deltaT nothing to do
    # (rounds back to 1). When deltat-scaling-enabled, phase1_iterations/
    # phase2_iterations must be passed through exactly as configured, not
    # floored by _settling_iterations - deltaT alone provides coverage.
    calls = []

    def fake_run_steady_state_scenario(*a, **k):
        calls.append(k)
        return {"reduction_pct": 90.0, "eACH_uv_steady_state": 50.0}
    monkeypatch.setattr(sr, "run_steady_state_scenario", fake_run_steady_state_scenario)

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"fan-enable": False, "inlet2-enable": False, "outlet2-enable": False,
                "inlet-wall": "xMin", "inlet-y-input": 1.5, "inlet-z-input": 1.3,
                "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 1500, "phase2-iterations": 1500, "target-t-ss": 1.0,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3,
                "monitoring-enable": False, "source-zone-size": 0.3}
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1,
           "plateau-rel-tol": 1.0, "t-infinity-early-stop-enabled": False, "phase1-require-stable-extrapolation": False, "keep-all-timesteps": False,
           "deltat-scaling-enabled": True, "deltat-effective-fraction": 0.7, "deltat-target-fraction": 0.995,
           "phase-chunk-size": 400, "phase-write-interval": 200}

    sr._run_scenario("case_dir", room, settings, z=6.0, ach=6.0, adv=adv,
                      z_summary={"eACH_uv_well_mixed_mean": 61.98, "fluence_mean": 1.0}, log_fn=lambda m: None,
                      should_stop=None, solver_log_fn=None)

    assert calls[-1]["phase1_iterations"] == 1500  # configured value, NOT _settling_iterations(6)=3179
    assert calls[-1]["phase2_iterations"] == 1500
    assert calls[-1]["phase1_delta_t"] == 3  # scaling actually engages against the true 1500 budget
    assert calls[-1]["phase2_delta_t"] == 1  # Phase 2's much higher effective rate needs no scaling


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
           "plateau-rel-tol": 1.0, "t-infinity-early-stop-enabled": False, "phase1-require-stable-extrapolation": False, "keep-all-timesteps": False,
           "deltat-scaling-enabled": False, "deltat-effective-fraction": 0.7, "deltat-target-fraction": 0.995,
           "phase-chunk-size": 400, "phase-write-interval": 200}

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
           "plateau-rel-tol": 1.0, "t-infinity-early-stop-enabled": False, "phase1-require-stable-extrapolation": False, "keep-all-timesteps": False,
           "deltat-scaling-enabled": False, "deltat-effective-fraction": 0.7, "deltat-target-fraction": 0.995,
           "phase-chunk-size": 400, "phase-write-interval": 200}

    sr._run_scenario("case_dir", room, settings, z=6.0, ach=3.0, adv=adv,
                      z_summary={"eACH_uv_well_mixed_mean": 0.0, "fluence_mean": 1.0}, log_fn=lambda m: None,
                      should_stop=None, solver_log_fn=None)

    assert calls[-1]["control_results_future"] is None


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
           "t-infinity-early-stop-enabled": False, "phase1-require-stable-extrapolation": False, "keep-all-timesteps": False,
           "phase-chunk-size": 400, "phase-write-interval": 200,
           "deltat-scaling-enabled": False, "deltat-effective-fraction": 0.7, "deltat-target-fraction": 0.995}

    sr._run_shared_phase1("base_dir", "phase1_dir", ach=3.0, room=room, settings=settings, adv=adv,
                           log_fn=lambda m: None, should_stop=None, solver_log_fn=None)

    assert copy_calls == [("base_dir", "phase1_dir")]
    assert len(scenario_calls) == 1
    call = scenario_calls[0]
    assert call["case_dir"] == "phase1_dir"
    assert call["ach"] == 3.0
    assert call["Z"] == 6  # placeholder - Phase 1 has no UV, so Z is irrelevant
    assert call["phase1_only"] is True


def test_build_flow_base_reuses_existing_resolved_base(tmp_path, monkeypatch):
    # Regression: a sweep that gets interrupted downstream of flow
    # convergence (e.g. by the old Phase1ExtrapolationUndecided gate)
    # left a fully-resolved _base_ACH<n> directory on disk. Re-running the
    # sweep must not pay for re-meshing/re-converging it - 0/fluenceRate
    # existing is the same "flow convergence fully resolved" signal
    # case_awaiting_flow_decision() already relies on.
    base_dir = tmp_path / "_base_ACH6"
    (base_dir / "0").mkdir(parents=True)
    (base_dir / "0" / "fluenceRate").write_text("dummy")

    setup_calls = []
    monkeypatch.setattr(sr, "setup_case", lambda *a, **k: setup_calls.append((a, k)))
    ach_delivery_calls = []
    monkeypatch.setattr(sr, "check_ach_delivery", lambda *a, **k: ach_delivery_calls.append((a, k)) or
                         {"ratio": 1.0, "measured_ach": 6.0})
    monkeypatch.setattr(sr, "read_cell_centers", lambda case_dir, time_dir: ["p1", "p2"])
    fluence_calls = []
    fake_fluence = np.array([1.0, 2.0])
    monkeypatch.setattr(sr, "compute_fluence_at_points",
                         lambda room, points: fluence_calls.append((room, points)) or fake_fluence)
    write_calls = []
    monkeypatch.setattr(sr, "write_scalar_field",
                         lambda case_dir, name, values, patch_names: write_calls.append((case_dir, name, values)))
    monkeypatch.setattr(sr, "read_boundary_patch_names", lambda case_dir: ["inlet", "outlet"])

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7, "lamps": ["lampA", "lampB"]})()
    settings = {"z-value": 6, "inlet-wall": "xMin", "inlet-y-input": 2.5, "inlet-z-input": 1.5,
                "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "outlet-wall": "xMax", "outlet-y-input": 2.5, "outlet-z-input": 1.5,
                "outlet-size-w": 0.3, "outlet-size-h": 0.3,
                "fan-enable": False, "inlet2-enable": False, "outlet2-enable": False}
    adv = {"mesh-cell-size": 0.1, "uv-zone-bins": 25, "flow-rel-tol": 1.0, "flow-max-iterations": 20000,
           "momentum-relaxation": None, "scalar-relaxation": None,
           "scalar-transport-ncorr": None, "scalar-transport-tolerance": None}

    result = sr._build_flow_base("guv_path", str(base_dir), room, settings, ach=6.0, adv=adv,
                                  log_fn=lambda m: None, should_stop=None, solver_log_fn=None)

    assert setup_calls == []
    assert result["reused"] is True
    assert result["inlet_velocity"] is not None
    assert result["n_lamps"] == 2  # reflects THIS run's room, not left stale as None
    # Regression guard (2026-08-11): fluenceRate is the one field on this
    # reuse path that depends on guv_path's lamp positions/power - unlike
    # every other reused field, it must be recomputed fresh from THIS
    # run's own room every time, never trusted from whatever .guv file
    # originally built/seeded this base (see _build_flow_base's own
    # docstring for the silent-wrong-results bug this closes).
    assert len(fluence_calls) == 1 and fluence_calls[0][0] is room
    assert write_calls == [(str(base_dir), "fluenceRate", fake_fluence)]
    # Regression guard (2026-08-10): the reuse path used to hardcode
    # ach_delivery=None, silently dropping ach_efficiency_pct/
    # mechanical_mixing_efficiency_pct from every combo sharing this ACH
    # whenever the flow field was reused (not just freshly solved) -
    # check_ach_delivery only reads already-solved fields, so there's no
    # reason to skip it just because setup_case() itself was skipped.
    assert len(ach_delivery_calls) == 1
    assert result["ach_delivery"] == {"ratio": 1.0, "measured_ach": 6.0}


def test_build_flow_base_reuse_skips_ach_delivery_check_when_sealed(tmp_path, monkeypatch):
    base_dir = tmp_path / "_base_ACHsealed"
    (base_dir / "0").mkdir(parents=True)
    (base_dir / "0" / "fluenceRate").write_text("dummy")
    monkeypatch.setattr(sr, "setup_case", lambda *a, **k: None)
    ach_delivery_calls = []
    monkeypatch.setattr(sr, "check_ach_delivery", lambda *a, **k: ach_delivery_calls.append(1))
    monkeypatch.setattr(sr, "read_cell_centers", lambda case_dir, time_dir: [])
    monkeypatch.setattr(sr, "compute_fluence_at_points", lambda room, points: np.array([]))
    monkeypatch.setattr(sr, "write_scalar_field", lambda *a, **k: None)
    monkeypatch.setattr(sr, "read_boundary_patch_names", lambda case_dir: [])

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7, "lamps": []})()
    settings = {"z-value": 6, "inlet-wall": "xMin", "inlet-y-input": 2.5, "inlet-z-input": 1.5,
                "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "outlet-wall": "xMax", "outlet-y-input": 2.5, "outlet-z-input": 1.5,
                "outlet-size-w": 0.3, "outlet-size-h": 0.3,
                "fan-enable": True, "inlet2-enable": False, "outlet2-enable": False}
    adv = {"mesh-cell-size": 0.1, "uv-zone-bins": 25, "flow-rel-tol": 1.0, "flow-max-iterations": 20000,
           "momentum-relaxation": None, "scalar-relaxation": None,
           "scalar-transport-ncorr": None, "scalar-transport-tolerance": None}

    result = sr._build_flow_base("guv_path", str(base_dir), room, settings, ach=0.0, adv=adv,
                                  log_fn=lambda m: None, should_stop=None, solver_log_fn=None, sealed=True)

    assert ach_delivery_calls == []
    assert result["ach_delivery"] is None


def test_run_shared_phase1_skips_entirely_when_checkpoint_already_exists(tmp_path, monkeypatch):
    # A checkpoint means Phase 1 already fully converged in an earlier
    # attempt - every Z clone downstream picks it up via
    # run_steady_state_scenario's own checkpoint-detection, so there's
    # nothing left for _run_shared_phase1 to do (and definitely no reason
    # to wipe phase1_dir via _copy_base_case first).
    phase1_dir = tmp_path / "_phase1_ACH6"
    phase1_dir.mkdir()
    sspl._write_phase1_checkpoint(str(phase1_dir), {"iterations": 1500}, {}, G=1.0, Su=1.0,
                                   source_volume=0.064, n_source_cells=64)

    copy_calls = []
    monkeypatch.setattr(sr, "_copy_base_case", lambda base, target, log_fn: copy_calls.append((base, target)))
    scenario_calls = []
    monkeypatch.setattr(sr, "run_steady_state_scenario", lambda *a, **k: scenario_calls.append((a, k)))
    monkeypatch.setattr(sr, "read_cell_centers", lambda case_dir, time_dir: [])
    monkeypatch.setattr(sr, "compute_fluence_at_points", lambda room, points: [])
    monkeypatch.setattr(sr, "write_scalar_field", lambda *a, **k: None)
    monkeypatch.setattr(sr, "read_boundary_patch_names", lambda case_dir: [])

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    sr._run_shared_phase1("base_dir", str(phase1_dir), ach=6.0, room=room, settings={}, adv={},
                           log_fn=lambda m: None, should_stop=None, solver_log_fn=None)

    assert copy_calls == []
    assert scenario_calls == []


def test_run_shared_phase1_resumes_undecided_pending_instead_of_restarting(tmp_path, monkeypatch):
    # Regression for the real stuck-sweep case: Phase 1 ran its full
    # budget and left phase1_pending.json (undecided, no checkpoint yet -
    # e.g. the old phase1_extrapolation_gate raised before checkpointing).
    # _copy_base_case must NOT run (it would rm -rf phase1_dir and destroy
    # this progress), and the resume should pass phase1_resume_decision=
    # "accept" so run_steady_state_scenario finalizes the existing state
    # instead of re-running the full phase1_iterations budget.
    phase1_dir = tmp_path / "_phase1_ACH6"
    phase1_dir.mkdir()
    sspl._write_phase1_pending(str(phase1_dir), G=1.0, Su=1.0, source_volume=0.064, n_source_cells=64)

    copy_calls = []
    monkeypatch.setattr(sr, "_copy_base_case", lambda base, target, log_fn: copy_calls.append((base, target)))
    scenario_calls = []
    def fake_run_steady_state_scenario(case_dir, room_x, room_y, room_z, ach, Z, **kwargs):
        scenario_calls.append({"case_dir": case_dir, **kwargs})
    monkeypatch.setattr(sr, "run_steady_state_scenario", fake_run_steady_state_scenario)
    monkeypatch.setattr(sr, "read_cell_centers", lambda case_dir, time_dir: [])
    monkeypatch.setattr(sr, "compute_fluence_at_points", lambda room, points: [])
    monkeypatch.setattr(sr, "write_scalar_field", lambda *a, **k: None)
    monkeypatch.setattr(sr, "read_boundary_patch_names", lambda case_dir: [])

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"fan-enable": False, "inlet2-enable": False, "outlet2-enable": False,
                "inlet-wall": "xMin", "inlet-y-input": 1.5, "inlet-z-input": 1.3,
                "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 1500, "target-t-ss": 1.0, "z-value": 6,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3,
                "t-ss-window-frac": None, "monitoring-enable": False, "source-zone-size": 0.3}
    adv = {"mesh-cell-size": 0.1, "plateau-rel-tol": 1.0,
           "t-infinity-early-stop-enabled": False, "phase1-require-stable-extrapolation": False,
           "keep-all-timesteps": False, "phase-chunk-size": 400, "phase-write-interval": 200,
           "deltat-scaling-enabled": False, "deltat-effective-fraction": 0.7, "deltat-target-fraction": 0.995}

    sr._run_shared_phase1(str(phase1_dir), str(phase1_dir), ach=6.0, room=room, settings=settings, adv=adv,
                           log_fn=lambda m: None, should_stop=None, solver_log_fn=None)

    assert copy_calls == []
    assert len(scenario_calls) == 1
    assert scenario_calls[0]["phase1_resume_decision"] == "accept"


# --- _run_shared_phase1's own fluenceRate recompute on reuse (2026-08-13
# incident: every Z/ACH combo for steady-state is cloned from phase1_dir,
# not base_dir directly - _build_flow_base's own recompute fix never
# reached the file that actually matters once Phase 1 itself is reused) ---

def test_run_shared_phase1_recomputes_fluence_when_checkpoint_reused(tmp_path, monkeypatch):
    phase1_dir = tmp_path / "_phase1_ACH6"
    phase1_dir.mkdir()
    sspl._write_phase1_checkpoint(str(phase1_dir), {"iterations": 1500}, {}, G=1.0, Su=1.0,
                                   source_volume=0.064, n_source_cells=64)
    monkeypatch.setattr(sr, "_copy_base_case", lambda *a, **k: None)
    monkeypatch.setattr(sr, "run_steady_state_scenario", lambda *a, **k: None)
    monkeypatch.setattr(sr, "read_cell_centers", lambda case_dir, time_dir: ["p1", "p2"])
    fluence_calls = []
    fake_fluence = [1.1, 2.2]
    monkeypatch.setattr(sr, "compute_fluence_at_points",
                         lambda room, points: fluence_calls.append((room, points)) or fake_fluence)
    write_calls = []
    monkeypatch.setattr(sr, "write_scalar_field",
                         lambda case_dir, name, values, patch_names: write_calls.append((case_dir, name, values)))
    monkeypatch.setattr(sr, "read_boundary_patch_names", lambda case_dir: ["inlet", "outlet"])

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    sr._run_shared_phase1("base_dir", str(phase1_dir), ach=6.0, room=room, settings={}, adv={},
                           log_fn=lambda m: None, should_stop=None, solver_log_fn=None)

    assert len(fluence_calls) == 1 and fluence_calls[0][0] is room
    assert write_calls == [(str(phase1_dir), "fluenceRate", fake_fluence)]


def test_run_shared_phase1_recomputes_fluence_when_resuming_pending(tmp_path, monkeypatch):
    phase1_dir = tmp_path / "_phase1_ACH6"
    phase1_dir.mkdir()
    sspl._write_phase1_pending(str(phase1_dir), G=1.0, Su=1.0, source_volume=0.064, n_source_cells=64)
    monkeypatch.setattr(sr, "_copy_base_case", lambda *a, **k: None)
    monkeypatch.setattr(sr, "run_steady_state_scenario", lambda *a, **k: None)
    monkeypatch.setattr(sr, "read_cell_centers", lambda case_dir, time_dir: ["p1"])
    fluence_calls = []
    fake_fluence = [3.3]
    monkeypatch.setattr(sr, "compute_fluence_at_points",
                         lambda room, points: fluence_calls.append((room, points)) or fake_fluence)
    write_calls = []
    monkeypatch.setattr(sr, "write_scalar_field",
                         lambda case_dir, name, values, patch_names: write_calls.append((case_dir, name, values)))
    monkeypatch.setattr(sr, "read_boundary_patch_names", lambda case_dir: [])

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"fan-enable": False, "inlet2-enable": False, "outlet2-enable": False,
                "inlet-wall": "xMin", "inlet-y-input": 1.5, "inlet-z-input": 1.3,
                "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 1500, "target-t-ss": 1.0, "z-value": 6,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3,
                "t-ss-window-frac": None, "monitoring-enable": False, "source-zone-size": 0.3}
    adv = {"mesh-cell-size": 0.1, "plateau-rel-tol": 1.0,
           "t-infinity-early-stop-enabled": False, "phase1-require-stable-extrapolation": False,
           "keep-all-timesteps": False, "phase-chunk-size": 400, "phase-write-interval": 200,
           "deltat-scaling-enabled": False, "deltat-effective-fraction": 0.7, "deltat-target-fraction": 0.995}

    sr._run_shared_phase1(str(phase1_dir), str(phase1_dir), ach=6.0, room=room, settings=settings, adv=adv,
                           log_fn=lambda m: None, should_stop=None, solver_log_fn=None)

    assert len(fluence_calls) == 1 and fluence_calls[0][0] is room
    assert write_calls == [(str(phase1_dir), "fluenceRate", fake_fluence)]


def test_run_sweep_different_guv_at_same_z_ach_produces_genuinely_different_results(tmp_path, monkeypatch):
    # End-to-end regression for the 2026-08-13 incident: a steady-state
    # sweep whose ACH group already has a REUSED flow base (_build_flow_base)
    # AND a REUSED Phase 1 checkpoint (_run_shared_phase1) - both genuinely
    # mode/guv-independent reuse paths - must still produce a DIFFERENT
    # combo result for a different .guv design, because both reuse paths
    # recompute their own copy of fluenceRate. A prior version of this fix
    # only covered _build_flow_base's own copy, which every combo's own
    # case_dir is NOT actually cloned from (phase1_dir is) - confirmed
    # live: n_lamps in the final report was correct, but the actual
    # UV-dependent numbers were byte-identical to the original design.
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    # _build_flow_base is mocked as "already reused" (fluenceRate already
    # present) - its own recompute isn't what's under test here.
    def fake_build_flow_base(guv_path, base_dir, room, settings, ach, adv, log_fn, *a, **k):
        Path(f"{base_dir}/0").mkdir(parents=True, exist_ok=True)
        Path(f"{base_dir}/0/fluenceRate").write_text("stale")
        return {"flow_converged": True, "inlet_velocity": 1.0, "inlet2_velocity": None, "n_lamps": 4}

    monkeypatch.setattr(sr, "_build_flow_base", fake_build_flow_base)
    monkeypatch.setattr(sr, "_run_shared_control", lambda *a, **k: {"total_ach_effective": 3.0})
    monkeypatch.setattr(sr, "write_source_topo_set_dict", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_copy_base_case",
                         lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})
    monkeypatch.setattr(sr, "compute_uv_fingerprint", lambda *a, **k: "fake-uv-fp")
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, *a, **k: None)

    # _run_shared_phase1 is the REAL function - already has an existing
    # checkpoint (simulating an earlier design's Phase 1), so it takes the
    # reuse branch and must recompute fluenceRate itself.
    phase1_dir = project_dir / "_phase1_ACH3"
    phase1_dir.mkdir()
    sspl._write_phase1_checkpoint(str(phase1_dir), {"iterations": 1500}, {}, G=1.0, Su=1.0,
                                   source_volume=0.064, n_source_cells=64)
    monkeypatch.setattr(sr, "read_cell_centers", lambda case_dir, time_dir: ["p1", "p2"])
    monkeypatch.setattr(sr, "read_boundary_patch_names", lambda case_dir: ["inlet", "outlet"])
    written_fluence = {}

    def fake_write_scalar_field(case_dir, name, values, patch_names):
        if name == "fluenceRate":
            written_fluence[case_dir] = values

    monkeypatch.setattr(sr, "write_scalar_field", fake_write_scalar_field)

    room_a = type("RoomA", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    room_b = type("RoomB", (), {"x": 4.0, "y": 5.0, "z": 2.7})()

    def fake_compute_fluence(room, points):
        # Genuinely different output per room, like two different lamp
        # layouts would produce.
        return [10.0] if room is room_a else [99.0]

    monkeypatch.setattr(sr, "compute_fluence_at_points", fake_compute_fluence)
    monkeypatch.setattr(sr, "_run_scenario", lambda *a, **k: {
        "reduction_pct": 90.0, "eACH_uv_steady_state": 50.0, "phase1": {"T_ss": 1.0, "live": {"t": [1]}},
        "phase2": {"T_ss": 0.1, "live": {"t": [1]}}})

    settings = {"sim-type": "steady_state", "fan-enable": False, "monitoring-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 100, "phase2-iterations": 100, "target-t-ss": 1.0,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3, "z-value": 6,
                "source-zone-size": 0.3}
    adv = {"uv-zone-bins": 25, "mesh-cell-size": 0.1}

    sr.run_sweep(
        guv_path="lampB.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room_b, settings=settings, adv=adv,
        z_values=[6], ach_values=[3], log_fn=lambda m: None,
    )

    # The recompute happened against phase1_dir (what every combo actually
    # clones from), using THIS run's own room (room_b) - not left at
    # whatever "stale" placeholder _build_flow_base's own reuse wrote.
    assert written_fluence.get(f"{project_dir}/_phase1_ACH3") == [99.0]


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
    monkeypatch.setattr(sr, "compute_uv_fingerprint", lambda *a, **k: "fake-uv-fp")
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
    monkeypatch.setattr(sr, "splice_live_vol_average_if_needed", lambda *a, **k: None)
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
    adv = {"uv-zone-bins": 5, "pimple-delta-t": 0.5, "max-co": 5,
           "decay-ach-min-fraction": 90.0, "decay-each-min-fraction": 90.0, "decay-each-max-fraction": 99.9}

    sr._run_decay_scenario(case_dir, room, settings, z=6.0, ach=3.0, adv=adv,
                            z_summary={"eACH_uv_well_mixed_mean": 20.0}, log_fn=lambda m: None,
                            should_stop=None, solver_log_fn=lambda m: None,
                            control_results_future=sr._completed_future(control_results))

    entries_z6 = written[case_dir]
    assert len(entries_z6) > 0

    # Now re-carve for a DIFFERENT Z on the same case dir (same as a 2nd
    # combination reusing the same ACH-group's copied case) and confirm
    # the rebuilt fvOptions entries actually change.
    sr._apply_z(case_dir, Z=1.0, nbins=5, fan_kwargs={}, log_fn=lambda m: None)
    sr._run_decay_scenario(case_dir, room, settings, z=1.0, ach=3.0, adv=adv,
                            z_summary={"eACH_uv_well_mixed_mean": 3.3}, log_fn=lambda m: None,
                            should_stop=None, solver_log_fn=lambda m: None,
                            control_results_future=sr._completed_future(control_results))
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
    monkeypatch.setattr(sr, "splice_live_vol_average_if_needed", lambda *a, **k: None)
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
    adv = {"uv-zone-bins": 5, "pimple-delta-t": 0.5, "max-co": 5,
           "decay-ach-min-fraction": 90.0, "decay-each-min-fraction": 90.0, "decay-each-max-fraction": 99.9}

    sr._run_decay_scenario(case_dir, room, settings, z=6.0, ach=3.0, adv=adv,
                            z_summary={"eACH_uv_well_mixed_mean": 20.0}, log_fn=lambda m: None,
                            should_stop=None, solver_log_fn=None,
                            control_results_future=sr._completed_future(control_results), status_fn=lambda k, m: status_calls.append((k, m)))

    key = "Z=6.0/ACH=3.0/UV-on"
    # No log_prefix wrapping here - status_key already carries the combo
    # identity, and the caller (app._poll_scenario) renders "[key] value"
    # itself, so double-prefixing here would just duplicate it.
    # "of ... total seconds" suffix - the exact total is this combo's own
    # combined_end_time (computed elsewhere, not re-derived/hardcoded
    # here) - just confirm the mechanism fired, not the specific number.
    matches = [m for k, m in status_calls if k == key and m and m.startswith("Time = 12.5 of ")]
    assert matches and matches[0].endswith(" total seconds")
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
    adv = {"pimple-delta-t": 0.5, "max-co": 5, "decay-ach-min-fraction": 99.9,
           "decay-each-max-fraction": 99.9, "decay-each-min-fraction": 90.0}

    sr._run_shared_control(str(tmp_path / "base"), str(tmp_path / "control"), ach=3.0, room=room,
                            settings=settings, adv=adv, log_fn=lambda m: None,
                            should_stop=None, solver_log_fn=None,
                            base_summary={"inlet_velocity": (0.1, 0.0, 0.0)},
                            status_fn=lambda k, m: status_calls.append((k, m)))

    key = "ACH=3.0/control"
    matches = [m for k, m in status_calls if k == key and m and m.startswith("Time = 5 of ")]
    assert matches and matches[0].endswith(" total seconds")
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
                         delta_t=None, max_co=None: control_time_calls.append(("main", write_interval)))
    monkeypatch.setattr(sr, "splice_live_vol_average_if_needed", lambda *a, **k: None)

    control_results = {"total_ach_effective": 3.0, "total_ach_effective_ci95": None,
                        "fit_se_per_s": None, "fit_n": None}
    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"fan-enable": False, "inlet2-enable": False, "outlet2-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "monitoring-enable": False, "pimple-write-interval": 3}
    # A duration long enough that duration // 100 (the old, wrong formula)
    # would clearly differ from the configured 3s.
    adv = {"uv-zone-bins": 5, "pimple-delta-t": 0.5, "max-co": 5,
           "decay-ach-min-fraction": 99.9, "decay-each-min-fraction": 99.9, "decay-each-max-fraction": 99.9}

    sr._run_decay_scenario(case_dir, room, settings, z=6.0, ach=3.0, adv=adv,
                            z_summary={"eACH_uv_well_mixed_mean": 20.0}, log_fn=lambda m: None,
                            should_stop=None, solver_log_fn=lambda m: None,
                            control_results_future=sr._completed_future(control_results))

    assert control_time_calls == [("main", 3)]


def test_run_shared_control_uses_configured_write_interval(tmp_path, monkeypatch):
    # Companion to the above: the UV-off control's write interval is now
    # set inside _run_shared_control (run once per ACH), not inside
    # _run_decay_scenario.
    prepare_calls = []
    monkeypatch.setattr(sr, "prepare_ventilation_only_control", lambda case_dir, control_dir, inlet_velocity,
                         end_time, write_interval, **k:
                         prepare_calls.append(("control", write_interval)))
    monkeypatch.setattr(sr, "run_wsl_streaming", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": ""})())
    monkeypatch.setattr(sr, "finish_ventilation_only_control", lambda *a, **k: {"total_ach_effective": 3.0})

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"inlet2-enable": False, "outlet2-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "pimple-write-interval": 3}
    adv = {"pimple-delta-t": 0.5, "max-co": 5, "decay-ach-min-fraction": 99.9,
           "decay-each-max-fraction": 99.9, "decay-each-min-fraction": 90.0}

    sr._run_shared_control(str(tmp_path / "base"), str(tmp_path / "control"), ach=3.0, room=room,
                            settings=settings, adv=adv, log_fn=lambda m: None,
                            should_stop=None, solver_log_fn=lambda m: None,
                            base_summary={"inlet_velocity": (0.1, 0.0, 0.0)})

    assert prepare_calls == [("control", 3)]


# --- _completed_future / _run_ach_pool (2026-08-11 concurrency redesign -
# see _MAX_CONCURRENT_SOLVES's own docstring for the full design: ONE
# shared, globally-capped solver pool instead of the old separate ACH/Z
# pools, with priority between stages falling out of submission order) ---

def test_completed_future_resolves_immediately_to_the_given_value():
    future = sr._completed_future({"total_ach_effective": 0.0})
    assert future.done()
    assert future.result() == {"total_ach_effective": 0.0}


def test_run_ach_pool_runs_every_ach_and_is_unbounded(monkeypatch):
    lock = threading.Lock()
    state = {"live": 0, "peak": 0}
    seen = []

    def ach_worker(ach):
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
        time.sleep(0.05)
        with lock:
            state["live"] -= 1
            seen.append(ach)

    achs = [1, 2, 3, 4, 5]
    sr._run_ach_pool(achs, ach_worker, should_stop=None)

    assert sorted(seen) == achs
    # Deliberately unbounded here (see _MAX_CONCURRENT_SOLVES's own
    # docstring - the real cap lives on the shared solver pool each
    # ach_worker submits its actual work to, not on this orchestrator
    # layer) - all 5 should have been able to run concurrently.
    assert state["peak"] == 5


def test_run_ach_pool_propagates_stopped_by_user():
    def ach_worker(ach):
        raise StoppedByUser("stop requested mid-combination")

    with pytest.raises(StoppedByUser):
        sr._run_ach_pool([3], ach_worker, should_stop=None)


def test_run_ach_pool_propagates_a_generic_exception():
    def ach_worker(ach):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        sr._run_ach_pool([3], ach_worker, should_stop=None)


def test_write_sweep_summary_csv_collects_every_done_combo_from_status(tmp_path):
    import csv as csv_module
    project_dir = str(tmp_path)
    project_name = "myproj"
    # Both combos recorded "done" in project_status.json, but only the
    # first actually produced a report.json - the second (failed/skipped
    # after being marked done some other way, or a stale record) has none
    # on disk, matching a real error/skip.
    from guvcfd.project_status import update_combo_status
    update_combo_status(project_dir, project_name, z=1.7, ach=3.0, status="done")
    update_combo_status(project_dir, project_name, z=6.0, ach=3.0, status="done")
    report1 = {
        "reduction_pct_corrected": 85.8, "eACH_uv_steady_state_corrected": 18.2,
        "eACH_uv_well_mixed": 20.0, "ach_delivery": {"measured_ach": 2.97, "ratio": 0.99},
    }
    with open(f"{project_dir}/{project_name}_{sr._subdir_name(1.7, 3.0)}_report.json", "w") as f:
        json.dump(report1, f)

    csv_path = sr.write_sweep_summary_csv(project_dir, project_name)

    assert csv_path == f"{project_dir}/{project_name}_sweep_summary.csv"
    with open(csv_path, newline="") as f:
        rows = list(csv_module.DictReader(f))
    assert len(rows) == 1  # the missing combo is omitted, not a blank row
    assert rows[0]["Z"] == "1.7"
    assert rows[0]["ACH"] == "3.0"
    assert float(rows[0]["total_reduction_pct"]) == pytest.approx(85.8)
    assert float(rows[0]["est_each_per_hr"]) == pytest.approx(18.2)


def test_write_sweep_summary_csv_includes_phase1_spatial_cov_and_phase2_t_ss_cv(tmp_path):
    # 2026-08-13: convergence-quality diagnostics - phase1's own SPATIAL
    # mixing uniformity (a snapshot at its final iteration) and phase2's
    # own TEMPORAL stability (how much the room-average T still
    # fluctuates over its trailing convergence window) - two different
    # questions, not the same number reused (see
    # _convergence_quality_columns's own docstring).
    import csv as csv_module
    project_dir = str(tmp_path)
    project_name = "myproj"
    from guvcfd.project_status import update_combo_status
    update_combo_status(project_dir, project_name, z=6.0, ach=3.0, status="done")
    report = {
        "reduction_pct_corrected": 80.0, "eACH_uv_steady_state_corrected": 10.0,
        "phase1": {"spatial_cov": 0.045}, "phase2": {"T_ss_cv": 0.012},
    }
    with open(f"{project_dir}/{project_name}_Z6_ACH3_report.json", "w") as f:
        json.dump(report, f)

    csv_path = sr.write_sweep_summary_csv(project_dir, project_name)
    with open(csv_path, newline="") as f:
        rows = list(csv_module.DictReader(f))

    assert len(rows) == 1
    assert float(rows[0]["phase1_spatial_cov_pct"]) == pytest.approx(4.5)
    assert float(rows[0]["phase2_T_ss_cv_pct"]) == pytest.approx(1.2)


def test_write_sweep_summary_csv_includes_convergence_flags(tmp_path):
    # 2026-08-17: phase1_converged/phase2_converged - a real incident
    # confirmed check_plateau_windowed's own "converged" result was
    # computed but never surfaced anywhere a user would see it across a
    # whole sweep (only a passive docx text label for a single run) - a
    # run whose curve never actually plateaued produced a clean-looking
    # reduction_pct with no visible caveat. Must be a real False (not
    # blank/None) when a phase genuinely ran and didn't converge, so a
    # sweep can be scanned for this directly.
    import csv as csv_module
    project_dir = str(tmp_path)
    project_name = "myproj"
    from guvcfd.project_status import update_combo_status
    update_combo_status(project_dir, project_name, z=6.0, ach=3.0, status="done")
    report = {
        "reduction_pct_corrected": 80.0, "eACH_uv_steady_state_corrected": 10.0,
        "phase1": {"converged": False}, "phase2": {"converged": True},
    }
    with open(f"{project_dir}/{project_name}_Z6_ACH3_report.json", "w") as f:
        json.dump(report, f)

    csv_path = sr.write_sweep_summary_csv(project_dir, project_name)
    with open(csv_path, newline="") as f:
        rows = list(csv_module.DictReader(f))

    assert len(rows) == 1
    # csv.DictWriter renders Python False/True as the literal strings
    # "False"/"True", not blank - confirms the flag is a real value, not
    # an accidentally-omitted column.
    assert rows[0]["phase1_converged"] == "False"
    assert rows[0]["phase2_converged"] == "True"


def test_write_sweep_summary_csv_blank_convergence_quality_for_decay_rows(tmp_path):
    # Decay-mode reports have no phase1/phase2 structure at all - these
    # columns must come back blank, not raise or default to 0.
    import csv as csv_module
    project_dir = str(tmp_path)
    project_name = "myproj"
    from guvcfd.project_status import update_combo_status
    update_combo_status(project_dir, project_name, z=6.0, ach=3.0, sim_type="decay", status="done")
    report = {"eACH_uv_effective": 12.0, "eACH_uv_well_mixed": 15.0}
    with open(f"{project_dir}/{project_name}_Z6_ACH3_report.json", "w") as f:
        json.dump(report, f)

    csv_path = sr.write_sweep_summary_csv(project_dir, project_name)
    with open(csv_path, newline="") as f:
        rows = list(csv_module.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["phase1_spatial_cov_pct"] == ""
    assert rows[0]["phase2_T_ss_cv_pct"] == ""


def test_write_sweep_summary_csv_includes_a_row_per_design_at_the_same_z_ach(tmp_path):
    import csv as csv_module
    project_dir = str(tmp_path)
    project_name = "myproj"
    from guvcfd.project_status import update_combo_status
    # Two different designs, both "done" at the SAME (z, ach) - see
    # compute_guv_design_suffix's own docstring for the incident this
    # guards against: deduplicating by (z, ach) alone used to collapse
    # these into a single row, always the ORIGINAL design's.
    update_combo_status(project_dir, project_name, z=6.0, ach=3.0, guv_path="lampA.guv",
                         sim_type="steady_state", subdir="Z6_ACH3", status="done")
    update_combo_status(project_dir, project_name, z=6.0, ach=3.0, guv_path="lampB.guv",
                         sim_type="steady_state", combo_suffix="_lampB", subdir="Z6_ACH3_lampB", status="done")

    with open(f"{project_dir}/{project_name}_Z6_ACH3_report.json", "w") as f:
        json.dump({"reduction_pct_corrected": 80.0, "eACH_uv_steady_state_corrected": 10.0}, f)
    with open(f"{project_dir}/{project_name}_Z6_ACH3_lampB_report.json", "w") as f:
        json.dump({"reduction_pct_corrected": 95.0, "eACH_uv_steady_state_corrected": 25.0}, f)

    csv_path = sr.write_sweep_summary_csv(project_dir, project_name)
    with open(csv_path, newline="") as f:
        rows = list(csv_module.DictReader(f))

    assert len(rows) == 2
    by_design = {r["Design"]: r for r in rows}
    assert set(by_design) == {"lampA", "lampB"}
    assert float(by_design["lampA"]["total_reduction_pct"]) == pytest.approx(80.0)
    assert float(by_design["lampB"]["total_reduction_pct"]) == pytest.approx(95.0)
    assert by_design["lampA"]["Mode"] == "steady_state" and by_design["lampB"]["Mode"] == "steady_state"


def test_write_sweep_summary_csv_includes_a_row_per_mode_at_the_same_z_ach(tmp_path):
    import csv as csv_module
    project_dir = str(tmp_path)
    project_name = "myproj"
    from guvcfd.project_status import update_combo_status
    # A steady-state project's Z/ACH re-evaluated in Decay mode - see
    # compute_sim_type_suffix's own docstring for the incident this
    # guards against, same class of bug as the guv-design case above.
    update_combo_status(project_dir, project_name, z=6.0, ach=3.0, guv_path="room.guv",
                         sim_type="steady_state", subdir="Z6_ACH3", status="done")
    update_combo_status(project_dir, project_name, z=6.0, ach=3.0, guv_path="room.guv",
                         sim_type="decay", combo_suffix="_decay", subdir="Z6_ACH3_decay", status="done")

    with open(f"{project_dir}/{project_name}_Z6_ACH3_report.json", "w") as f:
        json.dump({"reduction_pct_corrected": 80.0, "eACH_uv_steady_state_corrected": 10.0}, f)
    with open(f"{project_dir}/{project_name}_Z6_ACH3_decay_report.json", "w") as f:
        json.dump({"eACH_uv_effective": 12.0}, f)

    csv_path = sr.write_sweep_summary_csv(project_dir, project_name)
    with open(csv_path, newline="") as f:
        rows = list(csv_module.DictReader(f))

    assert len(rows) == 2
    by_mode = {r["Mode"]: r for r in rows}
    assert set(by_mode) == {"steady_state", "decay"}
    assert by_mode["steady_state"]["Design"] == "room" and by_mode["decay"]["Design"] == "room"


def test_write_sweep_summary_csv_handles_no_reports_at_all(tmp_path):
    csv_path = sr.write_sweep_summary_csv(str(tmp_path), "myproj")
    with open(csv_path) as f:
        content = f.read()
    assert "Z" in content  # header row still written
    assert len(content.strip().splitlines()) == 1  # no data rows


def test_write_sweep_summary_csv_preserves_combos_not_in_the_latest_sweep(tmp_path):
    # Regression guard for the 2026-08-10 incident: a follow-up sweep that
    # only touches ONE ACH must not wipe out previously-recorded rows for
    # other ACHs that this call never mentions - the CSV is rebuilt from
    # project_status.json's full history, not from any one call's own
    # combos list.
    import csv as csv_module
    project_dir, project_name = str(tmp_path), "myproj"
    from guvcfd.project_status import update_combo_status
    for z, ach in [(1.0, 1.5), (1.0, 3.0), (1.0, 6.0)]:
        update_combo_status(project_dir, project_name, z=z, ach=ach, status="done")
        with open(f"{project_dir}/{project_name}_{sr._subdir_name(z, ach)}_report.json", "w") as f:
            json.dump({"reduction_pct": 50.0, "eACH_uv_steady_state": 5.0}, f)

    # Simulate a later sweep that only ever touched ACH=3 (e.g. "add Z=7"
    # via the Extend/modify modal) calling this with no combos argument.
    sr.write_sweep_summary_csv(project_dir, project_name)

    with open(f"{project_dir}/{project_name}_sweep_summary.csv", newline="") as f:
        rows = list(csv_module.DictReader(f))
    achs_seen = {row["ACH"] for row in rows}
    assert achs_seen == {"1.5", "3.0", "6.0"}


def test_monitoring_summary_columns_steady_state_reduction_pct():
    detail = {
        "reduction_pct": 50.0,
        "monitoring": {"Point 1": {"phase1": {"T_ss": 10.0}, "phase2": {"T_ss": 4.0}}},
    }
    columns = sr._monitoring_summary_columns(detail)
    assert "Point 1_reduction_pct" in columns
    assert columns["Point 1_reduction_pct"] == pytest.approx(60.0)


def test_monitoring_summary_columns_decay_each_uv():
    detail = {
        "eACH_uv_effective": 5.0,
        "monitoring": {"Point 1": {"eACH_uv_effective": 4.2}},
    }
    columns = sr._monitoring_summary_columns(detail)
    assert columns == {"Point 1_eACH_uv": 4.2}


def test_monitoring_summary_columns_empty_when_no_monitoring():
    assert sr._monitoring_summary_columns({"reduction_pct": 50.0}) == {}


def test_write_sweep_summary_csv_includes_monitoring_point_columns(tmp_path):
    import csv as csv_module
    project_dir, project_name = str(tmp_path), "myproj"
    from guvcfd.project_status import update_combo_status
    update_combo_status(project_dir, project_name, z=1.0, ach=3.0, status="done")
    report = {
        "reduction_pct": 50.0, "eACH_uv_steady_state": 5.0,
        "monitoring": {"Point 1": {"phase1": {"T_ss": 10.0}, "phase2": {"T_ss": 4.0}}},
    }
    with open(f"{project_dir}/{project_name}_{sr._subdir_name(1.0, 3.0)}_report.json", "w") as f:
        json.dump(report, f)

    sr.write_sweep_summary_csv(project_dir, project_name)

    with open(f"{project_dir}/{project_name}_sweep_summary.csv", newline="") as f:
        rows = list(csv_module.DictReader(f))
    assert float(rows[0]["Point 1_reduction_pct"]) == pytest.approx(60.0)


# --- continue_decay (moved here from app.py 2026-08-10, so both apps share it) ---

class _FakeStreamResult:
    returncode = 0
    stdout = ""


def _make_case_dir_with_results(tmp_path, prior):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "results.json").write_text(json.dumps(prior))
    return str(case_dir)


def test_continue_decay_reads_back_via_the_post_hoc_path_it_wrote(tmp_path, monkeypatch):
    # Regression guard (2026-08-10): continue_decay runs a POST-HOC
    # `postProcess -dict system/volAverageDict` (writing to
    # postProcessing/volAverage1/0/...), but write_results_summary's own
    # default vol_average_dat points at the LIVE path
    # (postProcessing/volAverageLive1/...) a normal run's own solve writes
    # as it goes - without an explicit override, Continue would silently
    # read a stale/absent live file instead of the curve it just
    # recomputed.
    prior = {"ventilation_ach": 3.0, "eACH_uv_well_mixed": 5.0}
    case_dir = _make_case_dir_with_results(tmp_path, prior)

    monkeypatch.setattr(sr, "wsl_path", lambda p: p)
    monkeypatch.setattr(sr, "set_control_dict_start_from", lambda *a, **k: None)
    monkeypatch.setattr(sr, "set_control_dict_time", lambda *a, **k: None)
    monkeypatch.setattr(sr, "run_wsl_streaming", lambda *a, **k: _FakeStreamResult())
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda *a, **k: None)
    monkeypatch.setattr(sr, "read_latest_time_field", lambda *a, **k: [1.0, 1.0])
    monkeypatch.setattr(sr, "spatial_coefficient_of_variation", lambda values: 0.0)

    captured = {}

    def fake_write_results_summary(case_dir_, out_path, ventilation_ach, well_mixed_eACH_mean,
                                    vol_average_dat=None, **kwargs):
        captured["vol_average_dat"] = vol_average_dat
        return {"eACH_uv_effective": 1.0, "eACH_uv_well_mixed": well_mixed_eACH_mean}

    monkeypatch.setattr(sr, "write_results_summary", fake_write_results_summary)

    sr.continue_decay(case_dir, end_time=600, write_interval=10, log_fn=lambda m: None, should_stop=lambda: False)

    assert captured["vol_average_dat"] == "postProcessing/volAverage1/0/volFieldValue.dat"


def test_continue_decay_raises_when_no_prior_results(tmp_path):
    case_dir = tmp_path / "empty_case"
    case_dir.mkdir()
    with pytest.raises(RuntimeError, match="results.json"):
        sr.continue_decay(str(case_dir), end_time=600, write_interval=10)


def test_continue_decay_raises_stopped_by_user_when_stop_requested_mid_solve(tmp_path, monkeypatch):
    case_dir = _make_case_dir_with_results(tmp_path, {"ventilation_ach": 3.0, "eACH_uv_well_mixed": 5.0})
    monkeypatch.setattr(sr, "wsl_path", lambda p: p)
    monkeypatch.setattr(sr, "set_control_dict_start_from", lambda *a, **k: None)
    monkeypatch.setattr(sr, "set_control_dict_time", lambda *a, **k: None)
    monkeypatch.setattr(sr, "run_wsl_streaming", lambda *a, **k: _FakeStreamResult())

    with pytest.raises(StoppedByUser):
        sr.continue_decay(case_dir, end_time=600, write_interval=10, log_fn=lambda m: None,
                           should_stop=lambda: True)


def test_continue_decay_raises_on_pimplefoam_failure(tmp_path, monkeypatch):
    case_dir = _make_case_dir_with_results(tmp_path, {"ventilation_ach": 3.0, "eACH_uv_well_mixed": 5.0})
    monkeypatch.setattr(sr, "wsl_path", lambda p: p)
    monkeypatch.setattr(sr, "set_control_dict_start_from", lambda *a, **k: None)
    monkeypatch.setattr(sr, "set_control_dict_time", lambda *a, **k: None)

    class _Failed:
        returncode = 1
        stdout = "FOAM FATAL ERROR: boom"

    monkeypatch.setattr(sr, "run_wsl_streaming", lambda *a, **k: _Failed())

    with pytest.raises(RuntimeError, match="pimpleFoam failed"):
        sr.continue_decay(case_dir, end_time=600, write_interval=10, log_fn=lambda m: None,
                           should_stop=lambda: False)


# --- run_sweep/run_decay_sweep's own scheduling behavior (2026-08-11 - see
# _MAX_CONCURRENT_SOLVES's docstring): Phase 1 and control are genuine
# siblings for steady-state (both only need the converged flow base, not
# each other), and decay's control + every Z's own decay solve are siblings
# too (decay has no Phase 1 at all) - these guard that the rewrite actually
# runs them concurrently rather than accidentally serializing one behind
# the other, and that the shared solver pool's cap is real. ---

def _steady_state_settings():
    return {"sim-type": "steady_state", "fan-enable": False, "monitoring-enable": False,
            "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
            "phase1-iterations": 100, "phase2-iterations": 100, "target-t-ss": 1.0,
            "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3, "z-value": 6,
            "source-zone-size": 0.3}


def test_run_sweep_runs_phase1_and_control_concurrently(tmp_path, monkeypatch):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_copy_base_case", lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})
    monkeypatch.setattr(sr, "compute_uv_fingerprint", lambda *a, **k: "fake-uv-fp")
    monkeypatch.setattr(sr, "write_source_topo_set_dict", lambda *a, **k: None)
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_run_scenario", lambda *a, **k: {
        "reduction_pct": 90.0, "eACH_uv_steady_state": 50.0, "phase1": {"T_ss": 1.0, "live": {"t": [1]}},
        "phase2": {"T_ss": 0.1, "live": {"t": [1]}}})

    lock = threading.Lock()
    intervals = {}

    def make_recorder(name):
        def recorder(*a, **k):
            start = time.time()
            time.sleep(0.15)
            with lock:
                intervals[name] = (start, time.time())
            return {"total_ach_effective": 3.0} if name == "control" else None
        return recorder

    monkeypatch.setattr(sr, "_run_shared_phase1", make_recorder("phase1"))
    monkeypatch.setattr(sr, "_run_shared_control", make_recorder("control"))

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    sr.run_sweep(
        guv_path="proj.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=_steady_state_settings(), adv={"uv-zone-bins": 25, "mesh-cell-size": 0.1},
        z_values=[6], ach_values=[3], log_fn=lambda m: None,
    )

    p1_start, p1_end = intervals["phase1"]
    c_start, c_end = intervals["control"]
    # Genuinely concurrent, not sequential: each one's window overlaps the
    # other's, rather than one starting only after the other has finished.
    assert p1_start < c_end and c_start < p1_end


def test_run_decay_sweep_runs_control_and_z_decay_concurrently(tmp_path, monkeypatch):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_copy_base_case", lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda *a, **k: None)
    monkeypatch.setattr(sr, "compute_uv_fingerprint", lambda *a, **k: "fake-uv-fp")

    lock = threading.Lock()
    intervals = {}

    def fake_control(*a, **k):
        start = time.time()
        time.sleep(0.15)
        with lock:
            intervals["control"] = (start, time.time())
        return {"total_ach_effective": 3.0}

    def fake_decay_scenario(*a, **k):
        start = time.time()
        time.sleep(0.15)
        with lock:
            intervals["decay_z6"] = (start, time.time())
        return {"reduction_pct": 1.0, "eACH_uv_effective": 1.0, "eACH_uv_well_mixed": 1.0,
                "phase1": {}, "phase2": {}}

    monkeypatch.setattr(sr, "_run_shared_control", fake_control)
    monkeypatch.setattr(sr, "_run_decay_scenario", fake_decay_scenario)

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = dict(_decay_reuse_settings(), z_value=6)
    settings["pimple-write-interval"] = 3
    adv = {"uv-zone-bins": 5, "pimple-delta-t": 0.5, "max-co": 5,
           "decay-ach-min-fraction": 90.0, "decay-each-min-fraction": 90.0, "decay-each-max-fraction": 99.9}

    sr.run_decay_sweep(
        guv_path="p.guv", settings_path="p.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv=adv,
        z_values=[6], ach_values=[3], log_fn=lambda m: None,
    )

    c_start, c_end = intervals["control"]
    z_start, z_end = intervals["decay_z6"]
    assert c_start < z_end and z_start < c_end  # siblings, not sequential


def test_run_sweep_never_exceeds_max_concurrent_solves(tmp_path, monkeypatch):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.setattr(sr, "_MAX_CONCURRENT_SOLVES", 3)
    monkeypatch.setattr(sr, "_copy_base_case", lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})
    monkeypatch.setattr(sr, "compute_uv_fingerprint", lambda *a, **k: "fake-uv-fp")
    monkeypatch.setattr(sr, "write_source_topo_set_dict", lambda *a, **k: None)
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda *a, **k: None)

    lock = threading.Lock()
    state = {"live": 0, "peak": 0}

    def track(fn=None):
        def wrapped(*a, **k):
            with lock:
                state["live"] += 1
                state["peak"] = max(state["peak"], state["live"])
            time.sleep(0.03)
            with lock:
                state["live"] -= 1
            return fn(*a, **k) if fn else None
        return wrapped

    monkeypatch.setattr(sr, "_build_flow_base", track())
    monkeypatch.setattr(sr, "_run_shared_phase1", track())
    monkeypatch.setattr(sr, "_run_shared_control", track(lambda *a, **k: {"total_ach_effective": 3.0}))
    monkeypatch.setattr(sr, "_run_scenario", track(lambda *a, **k: {
        "reduction_pct": 90.0, "eACH_uv_steady_state": 50.0, "phase1": {"T_ss": 1.0, "live": {"t": [1]}},
        "phase2": {"T_ss": 0.1, "live": {"t": [1]}}}))

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    sr.run_sweep(
        guv_path="proj.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=_steady_state_settings(), adv={"uv-zone-bins": 25, "mesh-cell-size": 0.1},
        z_values=[2, 6], ach_values=[1.5, 3, 6], log_fn=lambda m: None,
    )

    assert state["peak"] <= 3
