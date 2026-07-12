import json

from guvcfd.app import _case_dir_has_data, _save_run_settings, _settings_mismatch, _MESH_AFFECTING_FIELDS


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
    assert set(saved.keys()) == set(_MESH_AFFECTING_FIELDS)
