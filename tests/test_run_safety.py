import json
from datetime import datetime

from guvcfd.app import (
    _ALWAYS_REQUIRED_FIELDS, _FAN_REQUIRED_FIELDS, _STEADY_STATE_REQUIRED_FIELDS,
    _INLET2_REQUIRED_FIELDS, _OUTLET2_REQUIRED_FIELDS, _NEW_FIELD_DEFAULTS,
    _WALL_POSITION_DIMS,
    _case_dir_has_data, _clear_setup_summary, _read_setup_summary, _record_run_timing,
    _save_run_settings, _settings_mismatch, _validate_settings, _write_setup_summary,
    case_awaiting_phase2_resume, _MESH_AFFECTING_FIELDS, SETTINGS_FIELDS,
)
from guvcfd.steady_state_pipeline import _write_phase1_checkpoint


def _settings(**overrides):
    base = {f: 1.0 for f in _MESH_AFFECTING_FIELDS}
    base.update(overrides)
    base["pimple-end-time"] = 120  # not a mesh-affecting field - must be ignored
    return base


def test_empty_dir_has_no_data(tmp_path):
    assert not _case_dir_has_data(str(tmp_path / "does-not-exist"))
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    assert not _case_dir_has_data(str(case_dir))


def test_results_json_counts_as_data(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "results.json").write_text("{}")
    assert _case_dir_has_data(str(case_dir))


def test_solver_time_directory_counts_as_data(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "0").mkdir()  # the fresh-run starting point - not "data" on its own
    assert not _case_dir_has_data(str(case_dir))
    (case_dir / "60").mkdir()  # a real solver time step
    assert _case_dir_has_data(str(case_dir))


def test_non_numeric_directory_is_ignored(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "system").mkdir()
    (case_dir / "constant").mkdir()
    assert not _case_dir_has_data(str(case_dir))


def test_no_prior_settings_file_means_no_mismatch(tmp_path):
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    assert _settings_mismatch(case_dir, _settings()) == []


def test_identical_settings_have_no_mismatch(tmp_path):
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    settings = _settings(ach=3.0, **{"fan-speed": 0.3})
    _save_run_settings(case_dir, settings)
    assert _settings_mismatch(case_dir, settings) == []


def test_changed_mesh_affecting_field_is_reported(tmp_path):
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    _save_run_settings(case_dir, _settings(ach=3.0))
    mismatches = _settings_mismatch(case_dir, _settings(ach=1.5))
    assert mismatches == [("ach", 3.0, 1.5)]


def test_pimple_end_time_change_is_never_reported(tmp_path):
    # Changing only the duration - the whole point of Continue - must not
    # trigger a mismatch warning.
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    _save_run_settings(case_dir, _settings())
    current = _settings()
    current["pimple-end-time"] = 999
    assert _settings_mismatch(case_dir, current) == []


def test_save_run_settings_only_persists_mesh_affecting_fields(tmp_path):
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    _save_run_settings(case_dir, _settings())
    with open(f"{case_dir}/run_settings.json") as f:
        saved = json.load(f)
    # monitoring_points and settings_path are saved too, under their own
    # keys - neither affects the mesh/flow field (see _save_run_settings),
    # they're there purely for report.py's case-setup preview/provenance.
    assert set(saved.keys()) == set(_MESH_AFFECTING_FIELDS) | {"monitoring_points", "settings_path"}
    assert saved["monitoring_points"] == []


def test_record_run_timing_adds_fields_to_results_json(tmp_path):
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    (tmp_path / "case" / "results.json").write_text(json.dumps({"reduction_pct": 50.0}))

    started_at = datetime(2026, 7, 13, 14, 30, 0)
    _record_run_timing(case_dir, started_at, 125.4)

    with open(f"{case_dir}/results.json") as f:
        saved = json.load(f)
    assert saved["reduction_pct"] == 50.0  # existing fields untouched
    assert saved["run_started_at"] == "2026-07-13T14:30:00"
    assert saved["run_elapsed_seconds"] == 125.4


def test_record_run_timing_is_a_noop_without_results_json(tmp_path):
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    # No results.json - should not raise.
    _record_run_timing(case_dir, datetime.now(), 10.0)
    assert not (tmp_path / "case" / "results.json").exists()


def _full_settings(**overrides):
    # Every field _validate_settings could ever require present and valid
    # (fan/steady-state fields included even though fan/monitoring start
    # off, so enabling them in a test doesn't spuriously report every field
    # in that group as missing) - the happy-path baseline per-test
    # overrides start from.
    base = {f: 1.0 for f in _ALWAYS_REQUIRED_FIELDS}
    base.update({f: 1.0 for f in _FAN_REQUIRED_FIELDS})
    base.update({f: 1.0 for f in _STEADY_STATE_REQUIRED_FIELDS})
    base.update({f: 1.0 for f in _INLET2_REQUIRED_FIELDS})
    base.update({f: 1.0 for f in _OUTLET2_REQUIRED_FIELDS})
    base["fan-direction"] = "down"
    base["sim-type"] = "decay"
    base["fan-enable"] = False
    base["monitoring-enable"] = False
    base["inlet2-enable"] = False
    base["outlet2-enable"] = False
    base.update(overrides)
    return base


def test_validate_settings_passes_when_all_required_fields_present():
    assert _validate_settings(_full_settings()) == []


def test_validate_settings_reports_missing_z_value():
    settings = _full_settings(**{"z-value": None})
    assert _validate_settings(settings) == ["UV inactivation constant Z"]


def test_validate_settings_reports_missing_ach():
    settings = _full_settings(ach=None)
    assert _validate_settings(settings) == ["Ventilation ACH"]


def test_validate_settings_ignores_fan_fields_when_fan_disabled():
    settings = _full_settings(**{"fan-enable": False, "fan-speed": None})
    assert _validate_settings(settings) == []


def test_validate_settings_requires_fan_fields_when_fan_enabled():
    settings = _full_settings(**{"fan-enable": True, "fan-speed": None})
    assert _validate_settings(settings) == ["Fan speed"]


def test_validate_settings_ignores_steady_state_fields_for_decay():
    settings = _full_settings(**{"sim-type": "decay", "target-t-ss": None})
    assert _validate_settings(settings) == []


def test_validate_settings_requires_steady_state_fields_for_steady_state():
    settings = _full_settings(**{"sim-type": "steady_state", "target-t-ss": None})
    assert _validate_settings(settings) == ["Target steady-state T"]


def test_validate_settings_ignores_disabled_monitoring_points():
    settings = _full_settings(**{
        "monitoring-enable": True,
        "monitor1-enable": False, "monitor1-x-input": None,
    })
    assert _validate_settings(settings) == []


def test_validate_settings_requires_enabled_monitoring_point_fields():
    settings = _full_settings(**{
        "monitoring-enable": True,
        "monitor1-enable": True, "monitor1-name": "Patient",
        "monitor1-x-input": None, "monitor1-y-input": 1.0,
        "monitor1-z-input": 1.0, "monitor1-cells": 4,
    })
    assert _validate_settings(settings) == ["Patient X position"]


def test_validate_settings_ignores_inlet2_fields_when_disabled():
    settings = _full_settings(**{"inlet2-enable": False, "inlet2-size-w": None})
    assert _validate_settings(settings) == []


def test_validate_settings_requires_inlet2_fields_when_enabled():
    settings = _full_settings(**{"inlet2-enable": True, "inlet2-size-w": None})
    assert _validate_settings(settings) == ["2nd inlet width"]


def test_validate_settings_requires_outlet2_fields_when_enabled():
    settings = _full_settings(**{"outlet2-enable": True, "outlet2-y-input": None})
    assert _validate_settings(settings) == ["2nd outlet Y position"]


def test_mesh_affecting_fields_includes_second_openings():
    # A 2nd inlet/outlet genuinely changes the mesh (an extra carved
    # patch) - unlike monitoring points, it must trigger Continue's
    # mismatch check.
    for field in ("inlet2-enable", "inlet2-wall", "inlet2-size-w", "inlet2-size-h",
                  "outlet2-enable", "outlet2-wall", "outlet2-size-w", "outlet2-size-h"):
        assert field in _MESH_AFFECTING_FIELDS


def test_changed_inlet2_wall_is_reported_as_mismatch(tmp_path):
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    _save_run_settings(case_dir, _settings(**{"inlet2-enable": True, "inlet2-wall": "ceiling"}))
    mismatches = _settings_mismatch(case_dir, _settings(**{"inlet2-enable": True, "inlet2-wall": "floor"}))
    assert ("inlet2-wall", "ceiling", "floor") in mismatches


def _restore_field_values(saved_settings):
    """Mirrors _open_project's own field_values construction exactly."""
    return dict(zip(SETTINGS_FIELDS, [
        saved_settings.get(fid, _NEW_FIELD_DEFAULTS.get(fid)) for fid in SETTINGS_FIELDS
    ]))


def test_opening_project_backfills_missing_second_opening_fields():
    # Regression test: opening a .guvcfd saved before the 2nd-inlet/2nd-
    # outlet feature existed left these keys missing from the file -
    # settings.get(fid) alone pushed a bare None into e.g. the wall
    # dropdown, which crashed _center_frac_for_wall
    # (_WALL_POSITION_DIMS[None] -> KeyError) the moment a 2nd opening was
    # enabled, since the dropdown never got a real value in the first place.
    old_settings = {"ach": 3.0, "z-value": 2.0}  # predates the feature entirely
    restored = _restore_field_values(old_settings)
    assert restored["inlet2-wall"] in _WALL_POSITION_DIMS
    assert restored["outlet2-wall"] in _WALL_POSITION_DIMS
    assert restored["inlet2-enable"] is False
    assert restored["outlet2-enable"] is False


def test_opening_project_does_not_override_explicitly_saved_second_opening_fields():
    saved_settings = {"outlet2-wall": "backWall", "outlet2-enable": True}
    restored = _restore_field_values(saved_settings)
    assert restored["outlet2-wall"] == "backWall"
    assert restored["outlet2-enable"] is True


def test_opening_project_backfills_missing_t_ss_window_frac():
    # A .guvcfd saved before the live-volAverage windowed-average feature
    # existed has no "t-ss-window-frac" key - must backfill to the same
    # 0.15 default the GUI field itself uses, not None.
    old_settings = {"ach": 3.0, "z-value": 2.0}
    restored = _restore_field_values(old_settings)
    assert restored["t-ss-window-frac"] == 0.15


# --- Steady-state Phase 2 resume: setup_summary.json checkpoint ---

def test_setup_summary_round_trips(tmp_path):
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    assert _read_setup_summary(case_dir) is None  # nothing yet

    summary = {"fluence_mean": 2.9, "eACH_uv_well_mixed_mean": 10.4,
               "flow_converged": True, "ach_delivery": {"within_tolerance": True}}
    _write_setup_summary(case_dir, summary)
    assert _read_setup_summary(case_dir) == summary


def test_setup_summary_cleared_removes_it(tmp_path):
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    _write_setup_summary(case_dir, {"fluence_mean": 1.0})
    assert _read_setup_summary(case_dir) is not None
    _clear_setup_summary(case_dir)
    assert _read_setup_summary(case_dir) is None


def test_setup_summary_clear_is_a_noop_when_absent(tmp_path):
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    _clear_setup_summary(case_dir)  # must not raise


def test_setup_summary_corrupted_file_reads_as_none(tmp_path):
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    (tmp_path / "case" / "setup_summary.json").write_text("{not valid json")
    assert _read_setup_summary(case_dir) is None


def test_no_resume_when_setup_never_completed(tmp_path):
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    assert case_awaiting_phase2_resume(case_dir) is None


def test_no_resume_when_run_already_finished(tmp_path):
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    _write_setup_summary(case_dir, {"fluence_mean": 1.0})
    (tmp_path / "case" / "results.json").write_text("{}")
    assert case_awaiting_phase2_resume(case_dir) is None


def test_resume_available_without_phase1_checkpoint(tmp_path):
    # Setup completed (mesh + flow convergence), but the scenario crashed
    # before Phase 1 of the two-phase steady-state run itself converged -
    # still resumable (skips setup_case(), but Phase 1 reruns).
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    _write_setup_summary(case_dir, {"fluence_mean": 1.0})
    info = case_awaiting_phase2_resume(case_dir)
    assert info == {"phase1_done": False, "phase1_iterations": None}


def test_resume_available_with_phase1_checkpoint(tmp_path):
    # Both setup AND Phase 1 already completed - resuming should be able to
    # report Phase 1's own iteration count too, so the panel can tell the
    # user just how much would be skipped.
    case_dir = str(tmp_path / "case")
    (tmp_path / "case").mkdir()
    _write_setup_summary(case_dir, {"fluence_mean": 1.0})
    _write_phase1_checkpoint(case_dir, {"T_ss": 1.05, "iterations": 12716}, {},
                              G=0.027, Su=1.5, source_volume=0.018, n_source_cells=18)
    info = case_awaiting_phase2_resume(case_dir)
    assert info == {"phase1_done": True, "phase1_iterations": 12716}
