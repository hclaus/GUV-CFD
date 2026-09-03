"""Project Setup tab UI behaviour that is easy to regress silently.

Covers the 2026-09-03 changes: a saved Notes field, monitoring points that
collapse when disabled, and the removal of the preview-only "-show" flag from
the mandatory inlet/outlet.
"""
import inspect

import pytest

from guvcfd.qtapp import project_setup_tab as pst
from guvcfd.qtapp import preview3d


# ------------------------------------------------------------------- notes
def test_notes_is_a_registered_saved_field():
    """It must go through _register so gather_settings/apply_settings carry it
    into the .guvcfd file - a notes box that is not saved is pointless."""
    src = inspect.getsource(pst.ProjectSetupTab._build_notes_group)
    assert '_register("notes"' in src


def test_notes_group_is_built_under_the_project_directory():
    src = inspect.getsource(pst.ProjectSetupTab.__init__)
    assert src.index("_build_case_dir_group") < src.index("_build_notes_group")


def test_notes_round_trip_through_get_and_set_value():
    """QPlainTextEdit must be handled by both accessors, or the field saves as
    None and silently loses the user's text."""
    for fn in (pst.ProjectSetupTab.get_value, pst.ProjectSetupTab.set_value):
        assert "QPlainTextEdit" in inspect.getsource(fn)


def test_notes_default_is_the_room_size():
    src = inspect.getsource(pst.ProjectSetupTab._default_notes_text)
    assert "room" in src.lower() and all(a in src for a in (".x", ".y", ".z"))


def test_prefill_never_overwrites_existing_notes():
    """A saved note must survive loading; prefill only fills a blank box."""
    src = inspect.getsource(pst.ProjectSetupTab._prefill_notes_if_empty)
    assert "strip()" in src and "not " in src


def test_prefill_runs_after_apply_settings_on_a_guvcfd_load():
    """Order matters: prefilling before the restore would be overwritten, and
    prefilling a blank box before apply_settings would then be clobbered."""
    src = inspect.getsource(pst.ProjectSetupTab.load_guvcfd_project)
    assert src.index("apply_settings") < src.index("_prefill_notes_if_empty")


# ------------------------------------------------- monitoring point collapse
def test_disabled_monitoring_points_hide_their_fields():
    src = inspect.getsource(pst.ProjectSetupTab._build_monitoring_group)
    assert "toggled.connect(fields.setVisible)" in src
    assert "setVisible(pt_enable.isChecked())" in src, "initial state must match too"


def test_bulk_restore_resyncs_monitor_visibility():
    """setChecked() only emits toggled when the value CHANGES, so restoring an
    already-matching value emits nothing and the blocks would keep stale
    visibility. apply_settings must resync explicitly."""
    assert "_sync_monitor_visibility" in inspect.getsource(pst.ProjectSetupTab.apply_settings)
    src = inspect.getsource(pst.ProjectSetupTab._sync_monitor_visibility)
    assert "isChecked()" in src and "setVisible" in src


# --------------------------------------------------------- the -show removal
def test_primary_openings_have_no_show_checkbox():
    src = inspect.getsource(pst.ProjectSetupTab._build_opening_group)
    assert '"{prefix}-show"' not in src and 'f"{prefix}-show"' not in src
    assert "Show in preview" not in src


def test_preview_always_draws_the_primary_openings():
    """They are mandatory in the mesh, so they must always be visible - and an
    older .guvcfd carrying inlet-show=false must not hide them."""
    src = inspect.getsource(preview3d.Preview3D.update_scene)
    assert 'settings.get("inlet-show"' not in src
    assert 'settings.get("outlet-show"' not in src
    assert '"inlet", "Inlet"' in src and '"outlet", "Outlet"' in src


def test_optional_elements_still_key_off_their_enable_flag():
    """The rule that survived: enabled -> visible, for everything optional."""
    src = inspect.getsource(preview3d.Preview3D.update_scene)
    for flag in ("inlet2-enable", "outlet2-enable", "fan-enable", "monitoring-enable"):
        assert f'settings.get("{flag}")' in src, flag
