from types import SimpleNamespace

from guvcfd import app as guvcfd_app


def _patient_ward_settings():
    return {
        "inlet-wall": "xMin", "inlet-y-input": 1.5, "inlet-z-input": 2.1,
        "inlet-size-w": 0.3, "inlet-size-h": 0.3,
        "outlet-wall": "xMax", "outlet-y-input": 1.5, "outlet-z-input": 0.4,
        "outlet-size-w": 0.3, "outlet-size-h": 0.3,
        "inject-x-input": 2.0, "inject-y-input": 1.5, "inject-z-input": 1.5,
        "source-zone-size": 0.3,
    }


def test_check_grid_alignment_flags_the_real_shrinking_case(monkeypatch):
    monkeypatch.setattr(guvcfd_app, "load_advanced_settings", lambda: {"mesh-cell-size": 0.1})
    room = SimpleNamespace(x=3.2, y=4.8, z=2.57)
    mismatches = guvcfd_app._check_grid_alignment(_patient_ward_settings(), room)
    names = {m["name"] for m in mismatches}
    assert names == {"Inlet", "Outlet", "Contaminant source zone"}


def test_check_grid_alignment_empty_when_already_grid_aligned(monkeypatch):
    monkeypatch.setattr(guvcfd_app, "load_advanced_settings", lambda: {"mesh-cell-size": 0.1})
    room = SimpleNamespace(x=4.0, y=4.0, z=4.0)
    settings = {
        "inlet-wall": "xMin", "inlet-y-input": 2.0, "inlet-z-input": 2.0,
        "inlet-size-w": 0.4, "inlet-size-h": 0.4,
        "outlet-wall": "xMax", "outlet-y-input": 2.0, "outlet-z-input": 2.0,
        "outlet-size-w": 0.4, "outlet-size-h": 0.4,
        "source-zone-size": 0.4,
    }
    assert guvcfd_app._check_grid_alignment(settings, room) == []


def test_check_grid_alignment_swallows_errors_on_malformed_settings(monkeypatch):
    monkeypatch.setattr(guvcfd_app, "load_advanced_settings", lambda: {"mesh-cell-size": 0.1})
    room = SimpleNamespace(x=3.2, y=4.8, z=2.57)
    # Missing every inlet/outlet field - shouldn't raise, just report nothing.
    assert guvcfd_app._check_grid_alignment({}, room) == []


def test_grid_align_modal_body_explains_why_consequences_and_changes():
    mismatches = [{"name": "Inlet", "nominal": (0.3, 0.3), "actual": (0.4, 0.4)}]
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
        {"name": "Inlet", "nominal": (0.3, 0.3), "actual": (0.4, 0.4)},
        {"name": "Contaminant source zone", "nominal": (0.3, 0.3, 0.3), "actual": (0.3, 0.4, 0.4)},
    ]
    is_open, note, *field_values = guvcfd_app._apply_grid_align_fix(1, "Loaded x.guv")

    values_by_id = dict(zip(guvcfd_app._GRID_ALIGN_ALL_FIELD_IDS, field_values))
    assert is_open is False
    assert "must be saved manually" in note
    assert values_by_id["inlet-size-w"] == 0.4
    assert values_by_id["inlet-size-h"] == 0.4
    assert values_by_id["source-zone-size"] == 0.4  # max of the actual (0.3, 0.4, 0.4)
    # Untouched fields stay dash.no_update, not overwritten with something else.
    import dash
    assert values_by_id["outlet-size-w"] is dash.no_update
    assert guvcfd_app._pending_grid_fix["mismatches"] is None


def test_keep_grid_align_as_typed_just_closes_modal_and_clears_pending():
    guvcfd_app._pending_grid_fix["mismatches"] = [
        {"name": "Inlet", "nominal": (0.3, 0.3), "actual": (0.4, 0.4)},
    ]
    is_open = guvcfd_app._keep_grid_align_as_typed(1)
    assert is_open is False
    assert guvcfd_app._pending_grid_fix["mismatches"] is None
