import json

import pytest

from guvcfd import app_settings
from guvcfd.app import (
    _SETTINGS_FIELD_IDS, _SETTINGS_FIELD_KEYS,
    _populate_settings_modal, _reset_settings_modal, _save_settings, _toggle_settings_modal,
)
from guvcfd.app_settings import ADVANCED_SETTINGS_DEFAULTS


@pytest.fixture(autouse=True)
def _isolated_settings_file(tmp_path, monkeypatch):
    # app.py's load_advanced_settings/save_advanced_settings are the same
    # function objects defined in app_settings.py, so patching the path
    # there redirects both modules' file I/O together.
    monkeypatch.setattr(app_settings, "ADVANCED_SETTINGS_PATH", tmp_path / "advanced_settings.json")
    return tmp_path / "advanced_settings.json"


def test_field_ids_and_keys_stay_aligned():
    # _populate_settings_modal/_reset_settings_modal/_save_settings all zip
    # these two lists together positionally - a length/order mismatch
    # would silently scramble which value goes in which field.
    assert len(_SETTINGS_FIELD_IDS) == len(_SETTINGS_FIELD_KEYS) == len(ADVANCED_SETTINGS_DEFAULTS)
    assert set(_SETTINGS_FIELD_KEYS) == set(ADVANCED_SETTINGS_DEFAULTS.keys())


def test_toggle_opens_and_closes_regardless_of_trigger():
    assert _toggle_settings_modal(1, None, None, False) is True   # menu-settings: closed -> open
    assert _toggle_settings_modal(None, 1, None, True) is False   # cancel: open -> closed
    assert _toggle_settings_modal(None, None, 1, True) is False   # save: open -> closed


def test_populate_reads_current_saved_values_in_field_order():
    custom = {**ADVANCED_SETTINGS_DEFAULTS, "flow-rel-tol": 3.3, "uv-zone-bins": 40}
    app_settings.save_advanced_settings(custom)

    values = _populate_settings_modal(1)
    as_dict = dict(zip(_SETTINGS_FIELD_KEYS, values))
    assert as_dict == custom


def test_reset_returns_hardcoded_defaults_in_field_order():
    values = _reset_settings_modal(1)
    as_dict = dict(zip(_SETTINGS_FIELD_KEYS, values))
    assert as_dict == ADVANCED_SETTINGS_DEFAULTS


def test_save_persists_given_values_and_confirms(_isolated_settings_file):
    new_values = [ADVANCED_SETTINGS_DEFAULTS[k] for k in _SETTINGS_FIELD_KEYS]
    new_values[_SETTINGS_FIELD_KEYS.index("mesh-cell-size")] = 0.05

    status = _save_settings(1, *new_values)

    assert status == "Saved."
    saved_on_disk = json.loads(_isolated_settings_file.read_text())
    assert saved_on_disk["mesh-cell-size"] == 0.05


def test_save_then_populate_round_trips(_isolated_settings_file):
    new_values = [ADVANCED_SETTINGS_DEFAULTS[k] for k in _SETTINGS_FIELD_KEYS]
    new_values[_SETTINGS_FIELD_KEYS.index("momentum-relaxation")] = 0.5

    _save_settings(1, *new_values)
    populated = _populate_settings_modal(1)

    assert populated[_SETTINGS_FIELD_KEYS.index("momentum-relaxation")] == 0.5
