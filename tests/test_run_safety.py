import json
from datetime import datetime

from guvcfd.app import (
    _ALWAYS_REQUIRED_FIELDS, _FAN_REQUIRED_FIELDS, _STEADY_STATE_REQUIRED_FIELDS,
    _INLET2_REQUIRED_FIELDS, _OUTLET2_REQUIRED_FIELDS,
    _case_dir_has_data, _record_run_timing, _save_run_settings, _settings_mismatch,
    _validate_settings, _MESH_AFFECTING_FIELDS,
)


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
    # monitoring_points is saved too, under its own key - it doesn't affect
    # the mesh/flow field (see _save_run_settings), it's there purely so
    # report.py's case-setup preview can draw monitoring points later.
    assert set(saved.keys()) == set(_MESH_AFFECTING_FIELDS) | {"monitoring_points"}
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
