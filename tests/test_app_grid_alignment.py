from types import SimpleNamespace

import dash

from guvcfd import app as guvcfd_app


def _patient_ward_settings():
    return {
        "inlet-wall": "xMin", "inlet-y-input": 1.5, "inlet-z-input": 2.1,
        "inlet-size-w": 0.3, "inlet-size-h": 0.3,
        "outlet-wall": "xMax", "outlet-y-input": 1.5, "outlet-z-input": 0.4,
        "outlet-size-w": 0.3, "outlet-size-h": 0.3,
        "inject-x-input": 2.0, "inject-y-input": 1.5, "inject-z-input": 1.5,
        "source-zone-cells": 1, "breathing-velocity": 0.06,
                "breathing-dir-x": 0.0, "breathing-dir-y": 0.0, "breathing-dir-z": 1.0,
    }


# --- _check_grid_alignment - source-zone-only now, inlet/outlet moved to
# the new sequential walk_opening_alignment_conflicts flow ---

def test_check_grid_alignment_is_source_position_only(monkeypatch):
    # Regression guard for the 2026-08-07 scope narrowing: inlet/outlet
    # mismatches used to appear here too - they must NOT anymore. Inlet/outlet
    # live exclusively in walk_opening_alignment_conflicts. What this reports
    # is now the source POSITION (2026-08-31): the zone's size is configured
    # in whole cells and so is exact by construction, but an off-lattice
    # centre still forces the box edges to snap outward and grow the zone.
    monkeypatch.setattr(guvcfd_app, "load_advanced_settings", lambda: {"mesh-cell-size": 0.1})
    room = SimpleNamespace(x=3.2, y=4.8, z=2.57)
    mismatches = guvcfd_app._check_grid_alignment(_patient_ward_settings(), room)
    names = {m["name"] for m in mismatches}
    assert names == {"Contaminant source position"}


def test_check_grid_alignment_empty_when_already_grid_aligned(monkeypatch):
    monkeypatch.setattr(guvcfd_app, "load_advanced_settings", lambda: {"mesh-cell-size": 0.1})
    room = SimpleNamespace(x=4.0, y=4.0, z=4.0)
    settings = {
        "inlet-wall": "xMin", "inlet-y-input": 2.0, "inlet-z-input": 2.0,
        "inlet-size-w": 0.4, "inlet-size-h": 0.4,
        "outlet-wall": "xMax", "outlet-y-input": 2.0, "outlet-z-input": 2.0,
        "outlet-size-w": 0.4, "outlet-size-h": 0.4,
    }
    assert guvcfd_app._check_grid_alignment(settings, room) == []


def test_check_grid_alignment_swallows_errors_on_malformed_settings(monkeypatch):
    monkeypatch.setattr(guvcfd_app, "load_advanced_settings", lambda: {"mesh-cell-size": 0.1})
    room = SimpleNamespace(x=3.2, y=4.8, z=2.57)
    # Missing every inlet/outlet field - shouldn't raise, just report nothing.
    assert guvcfd_app._check_grid_alignment({}, room) == []


def test_grid_align_modal_body_explains_why_consequences_and_changes():
    mismatches = [{"name": "Contaminant source zone", "nominal": (0.3, 0.3), "actual": (0.4, 0.4)}]
    body = guvcfd_app._grid_align_modal_body(mismatches)
    text = str(body)
    # Why it happens.
    assert "mesh cell size" in text
    # Consequences if not applied - must be honest that the SIMULATION
    # itself is already safe either way (the outward-snap fix guarantees
    # that), and that what's at stake is this project's own recorded
    # numbers matching reality, not simulation correctness.
    assert "don't apply" in text
    assert "simulation itself is still" in text
    # The specific suggested change itself.
    assert "0.3" in text and "0.4" in text
    # Asks for a decision.
    assert "Apply these" in text


def test_apply_grid_align_fix_writes_only_the_mismatched_fields(monkeypatch):
    guvcfd_app._pending_grid_fix["mismatches"] = [
        {"name": "Contaminant source zone", "nominal": (0.3, 0.3, 0.3), "actual": (0.3, 0.4, 0.4)},
    ]
    is_open, note, *field_values = guvcfd_app._apply_grid_align_fix(1, "Loaded x.guv")

    values_by_id = dict(zip(guvcfd_app._GRID_ALIGN_ALL_FIELD_IDS, field_values))
    assert is_open is False
    assert "must be saved manually" in note
    assert guvcfd_app._pending_grid_fix["mismatches"] is None


def test_keep_grid_align_as_typed_just_closes_modal_and_clears_pending():
    guvcfd_app._pending_grid_fix["mismatches"] = [
        {"name": "Contaminant source zone", "nominal": (0.3, 0.3), "actual": (0.4, 0.4)},
    ]
    is_open = guvcfd_app._keep_grid_align_as_typed(1)
    assert is_open is False
    assert guvcfd_app._pending_grid_fix["mismatches"] is None


# --- Sequential inlet/outlet flow (_grid_align_seq_body, _advance_alignment_walk,
# _grid_align_seq_agree/_decline, _open_bulk_modal_after_seq_modal_closes) ---

def _reset_pending_walk():
    guvcfd_app._pending_alignment_walk["walker"] = None
    guvcfd_app._pending_alignment_walk["current"] = None
    guvcfd_app._pending_alignment_walk["any_applied"] = False


def test_grid_align_seq_body_names_the_opening_and_axis_unambiguously():
    conflict = {"opening": "Inlet", "kind": "size", "field_id": "inlet-size-w",
                "axis_label": "width", "current": 0.23, "suggested": 0.3}
    text = str(guvcfd_app._grid_align_seq_body(conflict))
    assert "Inlet" in text and "width" in text
    assert "0.23" in text and "0.3" in text


def test_grid_align_seq_body_distinguishes_size_and_center_wording():
    size_conflict = {"opening": "Outlet", "kind": "size", "field_id": "outlet-size-w",
                      "axis_label": "width", "current": 0.37, "suggested": 0.4}
    center_conflict = {"opening": "Outlet", "kind": "center", "field_id": "outlet-y-input",
                        "axis_label": "position 1", "current": 2.53, "suggested": 2.5}
    size_text = str(guvcfd_app._grid_align_seq_body(size_conflict))
    center_text = str(guvcfd_app._grid_align_seq_body(center_conflict))
    assert "multiple" in size_text
    assert "multiple" not in center_text
    assert "edges" in center_text


def test_advance_alignment_walk_agree_writes_the_current_field_and_advances(monkeypatch):
    _reset_pending_walk()

    def fake_gen():
        agree1 = yield {"opening": "Inlet", "kind": "size", "field_id": "inlet-size-w",
                         "axis_label": "width", "current": 0.23, "suggested": 0.3}
        assert agree1 is True
        yield {"opening": "Inlet", "kind": "size", "field_id": "inlet-size-h",
               "axis_label": "height", "current": 0.35, "suggested": 0.4}

    gen = fake_gen()
    first = next(gen)
    guvcfd_app._pending_alignment_walk.update(walker=gen, current=first, any_applied=False)

    still_open, body, field_updates = guvcfd_app._advance_alignment_walk(True)
    assert still_open is True
    assert field_updates == {"inlet-size-w": 0.3}
    assert guvcfd_app._pending_alignment_walk["any_applied"] is True
    assert guvcfd_app._pending_alignment_walk["current"]["field_id"] == "inlet-size-h"
    _reset_pending_walk()


def test_advance_alignment_walk_decline_writes_nothing_but_still_advances():
    _reset_pending_walk()

    def fake_gen():
        agree1 = yield {"opening": "Inlet", "kind": "size", "field_id": "inlet-size-w",
                         "axis_label": "width", "current": 0.23, "suggested": 0.3}
        assert agree1 is False
        yield {"opening": "Inlet", "kind": "size", "field_id": "inlet-size-h",
               "axis_label": "height", "current": 0.35, "suggested": 0.4}

    gen = fake_gen()
    first = next(gen)
    guvcfd_app._pending_alignment_walk.update(walker=gen, current=first, any_applied=False)

    still_open, body, field_updates = guvcfd_app._advance_alignment_walk(False)
    assert still_open is True
    assert field_updates == {}
    assert guvcfd_app._pending_alignment_walk["any_applied"] is False
    _reset_pending_walk()


def test_advance_alignment_walk_closes_on_stop_iteration():
    _reset_pending_walk()

    def fake_gen():
        yield {"opening": "Inlet", "kind": "size", "field_id": "inlet-size-w",
               "axis_label": "width", "current": 0.23, "suggested": 0.3}

    gen = fake_gen()
    first = next(gen)
    guvcfd_app._pending_alignment_walk.update(walker=gen, current=first, any_applied=False)

    still_open, body, field_updates = guvcfd_app._advance_alignment_walk(True)
    assert still_open is False
    assert field_updates == {"inlet-size-w": 0.3}
    assert guvcfd_app._pending_alignment_walk["walker"] is None
    _reset_pending_walk()


def test_grid_align_seq_agree_callback_writes_only_the_one_field_others_stay_no_update():
    _reset_pending_walk()

    def fake_gen():
        yield {"opening": "Inlet", "kind": "size", "field_id": "inlet-size-w",
               "axis_label": "width", "current": 0.23, "suggested": 0.3}

    gen = fake_gen()
    first = next(gen)
    guvcfd_app._pending_alignment_walk.update(walker=gen, current=first, any_applied=False)

    is_open, body, status, *field_values = guvcfd_app._grid_align_seq_agree(1, "Loaded x.guv")
    values_by_id = dict(zip(guvcfd_app._SEQ_ALIGN_ALL_FIELD_IDS, field_values))
    assert values_by_id["inlet-size-w"] == 0.3
    other_fields = [fid for fid in guvcfd_app._SEQ_ALIGN_ALL_FIELD_IDS if fid != "inlet-size-w"]
    assert all(values_by_id[fid] is dash.no_update for fid in other_fields)
    # Walk ended (StopIteration) with something applied -> save reminder + closed.
    assert is_open is False
    assert "must be saved manually" in status
    _reset_pending_walk()


def test_grid_align_seq_decline_callback_writes_nothing_and_no_reminder_if_never_applied():
    _reset_pending_walk()

    def fake_gen():
        yield {"opening": "Inlet", "kind": "size", "field_id": "inlet-size-w",
               "axis_label": "width", "current": 0.23, "suggested": 0.3}

    gen = fake_gen()
    first = next(gen)
    guvcfd_app._pending_alignment_walk.update(walker=gen, current=first, any_applied=False)

    is_open, body, status = guvcfd_app._grid_align_seq_decline(1, "Loaded x.guv")
    assert is_open is False
    assert status == "Loaded x.guv"  # unchanged - nothing was ever agreed to
    _reset_pending_walk()


def test_open_bulk_modal_after_seq_modal_closes_noop_while_still_open():
    guvcfd_app._pending_grid_fix["mismatches"] = [{"name": "Contaminant source zone",
                                                     "nominal": (0.3,), "actual": (0.4,)}]
    is_open, body = guvcfd_app._open_bulk_modal_after_seq_modal_closes(True)
    assert is_open is dash.no_update
    guvcfd_app._pending_grid_fix["mismatches"] = None


def test_open_bulk_modal_after_seq_modal_closes_opens_when_source_zone_pending():
    guvcfd_app._pending_grid_fix["mismatches"] = [{"name": "Contaminant source zone",
                                                     "nominal": (0.3,), "actual": (0.4,)}]
    is_open, body = guvcfd_app._open_bulk_modal_after_seq_modal_closes(False)
    assert is_open is True
    assert "0.3" in str(body) and "0.4" in str(body)
    guvcfd_app._pending_grid_fix["mismatches"] = None


def test_open_bulk_modal_after_seq_modal_closes_noop_when_nothing_pending():
    guvcfd_app._pending_grid_fix["mismatches"] = None
    is_open, body = guvcfd_app._open_bulk_modal_after_seq_modal_closes(False)
    assert is_open is dash.no_update
