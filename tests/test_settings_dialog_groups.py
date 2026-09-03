"""Advanced Settings dialog: grouping and number resolution.

Roughly half these settings do nothing in the mode being run - every phase1-*
knob is inert in decay, every decay-* knob inert in steady state - and a flat
list of 40 gave no way to tell which. They are now grouped by the mode they
affect.
"""
import pytest

from guvcfd.app_settings import ADVANCED_SETTINGS_DEFAULTS
from guvcfd.qtapp.settings_dialog import (SETTING_GROUPS, DIALOG_HIDDEN_SETTINGS,
                                          decimals_for, _FIELD_INFO)


def _grouped():
    return [k for _, keys in SETTING_GROUPS for k in keys]


def test_every_setting_is_either_grouped_or_deliberately_hidden():
    g = _grouped()
    assert len(g) == len(set(g)), "a setting appears in two groups"
    assert sorted(set(g) | DIALOG_HIDDEN_SETTINGS) == sorted(ADVANCED_SETTINGS_DEFAULTS),         "a setting is neither grouped nor explicitly hidden - it would vanish from the dialog"
    assert not (set(g) & DIALOG_HIDDEN_SETTINGS), "a hidden setting is also grouped"


def test_hidden_deltat_settings_remain_in_the_defaults():
    """They must stay in ADVANCED_SETTINGS_DEFAULTS even though the dialog no
    longer edits them - merge_project_deltat_settings falls back to adv[key]
    for a project saved before those fields existed."""
    for key in DIALOG_HIDDEN_SETTINGS:
        assert key in ADVANCED_SETTINGS_DEFAULTS, f"{key} still needed as the fallback"


def test_the_hidden_ones_are_exactly_the_duplicated_deltat_trio():
    """They are editable in the Project Setup tab's Simulation Settings dialog,
    and merge_project_deltat_settings resolves settings.get(key, adv[key]) - so
    the project value always wins and a second control here would be dead."""
    assert DIALOG_HIDDEN_SETTINGS == {
        "deltat-scaling-enabled", "deltat-effective-fraction", "deltat-target-fraction"}
    import inspect
    from guvcfd import steady_state_pipeline as ssp
    src = inspect.getsource(ssp.merge_project_deltat_settings)
    assert "settings.get(key, adv[key])" in src, "the project-wins rule changed"


def test_no_group_references_a_setting_that_no_longer_exists():
    for key in list(_grouped()) + list(DIALOG_HIDDEN_SETTINGS):
        assert key in ADVANCED_SETTINGS_DEFAULTS, f"{key} was removed but is still grouped"


def test_the_three_expected_groups_exist():
    titles = [t for t, _ in SETTING_GROUPS]
    assert titles == ["Decay-mode only", "Steady-state only", "General (both modes)"]


def test_every_setting_still_has_a_label_and_tooltip():
    for key in ADVANCED_SETTINGS_DEFAULTS:
        label, tip = _FIELD_INFO.get(key, (None, None))
        assert label, f"{key} has no label"
        assert tip, f"{key} has no hover help"


# ------------------------------------------------------------ number format
def test_two_decimals_is_the_default():
    for v in (0.5, 1.0, 99.9, 10.0, 1.3, 2.5):
        assert decimals_for(v) == 2


def test_small_defaults_keep_enough_precision_to_survive():
    """A blanket 2 dp would round scalar-transport-tolerance (1e-4) to 0.00 and
    destroy it the first time the dialog was opened and saved."""
    assert decimals_for(0.0001) == 4
    assert decimals_for(0.995) == 3


@pytest.mark.parametrize("key,default", [
    (k, v) for k, v in ADVANCED_SETTINGS_DEFAULTS.items() if isinstance(v, float)])
def test_no_float_default_is_lost_at_its_displayed_precision(key, default):
    """Round-trip every float default through its own spin-box precision."""
    shown = round(float(default), decimals_for(default))
    assert shown == pytest.approx(float(default)), f"{key} would be altered by the dialog"


def test_decay_and_steady_groups_do_not_overlap():
    decay = set(dict(SETTING_GROUPS)["Decay-mode only"])
    steady = set(dict(SETTING_GROUPS)["Steady-state only"])
    assert not (decay & steady)
    # t-clamp is spliced into BOTH the decay run and Phase 2, so it belongs
    # in neither "only" group - it was wrongly filed under decay at first.
    general = set(dict(SETTING_GROUPS)["General (both modes)"])
    assert "t-clamp-decay-enabled" in general and "t-clamp-decay-multiplier" in general
    # the two relaxation settings are the ones most easily confused
    assert "decay-scalar-relaxation" in decay
    assert "scalar-relaxation" in steady
