"""Loading a project must rebuild the 3D preview ONCE, not once per field.

Every field widget is connected to refresh_preview (see
_connect_preview_refresh) and refresh_preview does a FULL PyVista rebuild -
plotter.clear() then re-adding the room box, floor, wall labels, every lamp,
every opening and the fan. Restoring ~25 fields one at a time therefore tore
down and redrew the scene ~25 times, which the user saw as the preview
flickering for ~30 seconds every time a .guvcfd was opened.

These tests exercise the coalescing logic directly rather than through Qt, so
they run headless on both forks.
"""
import inspect

import pytest

from guvcfd.qtapp import project_setup_tab as pst


class _Recorder:
    """Minimal stand-in exposing just the suspension machinery."""
    _preview_suspended = pst.ProjectSetupTab._preview_suspended
    refresh_preview = pst.ProjectSetupTab.refresh_preview

    def __init__(self):
        self._preview_suspend_depth = 0
        self.rebuilds = 0
        self.room = None

    def gather_settings(self):
        return {}


def _make():
    r = _Recorder()
    # count real rebuilds: refresh_preview returns early while suspended,
    # otherwise it reaches update_scene
    class _P:
        def update_scene(self, room, settings):
            r.rebuilds += 1
    r.preview = _P()
    return r


def test_unsuspended_refresh_rebuilds():
    r = _make()
    r.refresh_preview()
    r.refresh_preview()
    assert r.rebuilds == 2


def test_suspended_refreshes_are_dropped():
    r = _make()
    with r._preview_suspended():
        for _ in range(25):          # ~ one per restored field
            r.refresh_preview()
    assert r.rebuilds == 0, "field updates must not rebuild the scene"
    r.refresh_preview()              # the single explicit refresh afterwards
    assert r.rebuilds == 1


def test_nesting_unwinds_correctly():
    """load_guvcfd_project suspends, then calls apply_settings which suspends
    again - the inner exit must NOT re-enable rebuilds early."""
    r = _make()
    with r._preview_suspended():
        with r._preview_suspended():
            r.refresh_preview()
        r.refresh_preview()          # still inside the outer suspension
        assert r.rebuilds == 0
    r.refresh_preview()
    assert r.rebuilds == 1


def test_suspension_lifts_even_if_the_body_raises():
    r = _make()
    with pytest.raises(ValueError):
        with r._preview_suspended():
            raise ValueError("mid-load failure")
    assert r._preview_suspend_depth == 0
    r.refresh_preview()
    assert r.rebuilds == 1, "a failed load must not leave the preview dead"


@pytest.mark.parametrize("fn", ["apply_settings", "load_guvcfd_project", "load_project"])
def test_bulk_field_paths_are_suspended(fn):
    """The three routines that set many fields at once must all coalesce."""
    src = inspect.getsource(getattr(pst.ProjectSetupTab, fn))
    assert "_preview_suspended()" in src, f"{fn} rebuilds the scene per field"
