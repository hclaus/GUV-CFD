import json

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
    monkeypatch.setattr(sr, "_copy_base_case", lambda base, target, log_fn: __import__("os").makedirs(target, exist_ok=True))
    monkeypatch.setattr(sr, "_apply_z", lambda case_dir, z, nbins, fan_kwargs, log_fn:
                         {"fluence_mean": 1.0, "eACH_uv_well_mixed_mean": 0.0})

    def fake_run_scenario(case_dir, room, settings, z, ach, adv, z_summary, log_fn, should_stop, solver_log_fn):
        return {"reduction_pct": 90.0, "eACH_uv_steady_state": 50.0, "phase1": {"T_ss": 1.0, "live": {"t": [1]}},
                "phase2": {"T_ss": 0.1, "live": {"t": [1]}}}
    monkeypatch.setattr(sr, "_run_scenario", fake_run_scenario)

    removed = []
    monkeypatch.setattr(sr, "run_wsl_or_raise", lambda cmd, *a, **k: removed.append(cmd))

    room = type("Room", (), {"x": 4.0, "y": 5.0, "z": 2.7})()
    settings = {"sim-type": "steady_state", "fan-enable": False, "monitoring-enable": False,
                "inlet-wall": "xMin", "inlet-size-w": 0.3, "inlet-size-h": 0.3,
                "phase1-iterations": 100, "phase2-iterations": 100, "target-t-ss": 1.0,
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3, "z-value": 6}
    adv = {"uv-zone-bins": 25}

    results_seen = []
    sr.run_sweep(
        guv_path="proj.guv", settings_path="proj.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv=adv,
        z_values=[2, 6], ach_values=[3], log_fn=lambda m: None,
        on_combo_done=lambda z, ach, status, detail: results_seen.append((z, ach, status)),
    )

    assert build_calls == [3]  # one flow base built, for ACH=3
    assert results_seen == [(2, 3, "done"), (6, 3, "done")]
    assert (project_dir / "myproject_Z2_ACH3_report.json").exists()
    assert (project_dir / "myproject_Z6_ACH3_report.json").exists()
    trimmed = json.loads((project_dir / "myproject_Z2_ACH3_report.json").read_text())
    assert "live" not in trimmed["phase1"]
    assert trimmed["reduction_pct"] == 90.0
    assert any("_base_ACH3" in cmd for cmd in removed)  # base dir cleanup happened


def test_run_sweep_skips_failed_combo_and_continues(tmp_path, monkeypatch):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: None)
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
                "inject-x-input": 2, "inject-y-input": 2.5, "inject-z-input": 1.3, "z-value": 6}

    seen = []
    sr.run_sweep(
        guv_path="p.guv", settings_path="p.guvcfd", project_dir=str(project_dir),
        room=room, settings=settings, adv={"uv-zone-bins": 25},
        z_values=[2, 6], ach_values=[3], log_fn=lambda m: None,
        on_combo_done=lambda z, ach, status, detail: seen.append((z, status)),
    )

    assert seen == [(2, "error"), (6, "done")]  # combo 1 failed, sweep continued to combo 2


def test_run_sweep_stop_between_combinations_raises_stopped_by_user(tmp_path, monkeypatch):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.setattr(sr, "_build_flow_base", lambda *a, **k: None)
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
