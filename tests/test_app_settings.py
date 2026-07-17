import json

import pytest

from guvcfd import app_settings
from guvcfd.app_settings import ADVANCED_SETTINGS_DEFAULTS, load_advanced_settings, save_advanced_settings


@pytest.fixture(autouse=True)
def _isolated_settings_file(tmp_path, monkeypatch):
    # Never touch the real advanced_settings.json at the repo root while
    # testing - point the module at a throwaway path per test instead.
    monkeypatch.setattr(app_settings, "ADVANCED_SETTINGS_PATH", tmp_path / "advanced_settings.json")
    return tmp_path / "advanced_settings.json"


def test_load_returns_defaults_when_no_file_exists():
    assert load_advanced_settings() == ADVANCED_SETTINGS_DEFAULTS


def test_save_then_load_round_trips_exactly(_isolated_settings_file):
    custom = {**ADVANCED_SETTINGS_DEFAULTS, "flow-rel-tol": 2.5, "mesh-cell-size": 0.05}
    save_advanced_settings(custom)
    assert load_advanced_settings() == custom


def test_load_backfills_missing_keys_from_an_older_partial_file(_isolated_settings_file):
    # Simulates a file saved by an older version that predates a new field
    # being added to ADVANCED_SETTINGS_DEFAULTS.
    partial = dict(ADVANCED_SETTINGS_DEFAULTS)
    del partial["uv-zone-bins"]
    _isolated_settings_file.write_text(json.dumps(partial))

    loaded = load_advanced_settings()
    assert loaded["uv-zone-bins"] == ADVANCED_SETTINGS_DEFAULTS["uv-zone-bins"]
    assert loaded["flow-rel-tol"] == partial["flow-rel-tol"]


def test_save_drops_unknown_extra_keys(_isolated_settings_file):
    save_advanced_settings({**ADVANCED_SETTINGS_DEFAULTS, "some-stray-field": 999})
    saved_on_disk = json.loads(_isolated_settings_file.read_text())
    assert "some-stray-field" not in saved_on_disk
    assert set(saved_on_disk.keys()) == set(ADVANCED_SETTINGS_DEFAULTS.keys())


def test_load_falls_back_to_defaults_on_corrupted_json(_isolated_settings_file):
    _isolated_settings_file.write_text("{not valid json")
    assert load_advanced_settings() == ADVANCED_SETTINGS_DEFAULTS
