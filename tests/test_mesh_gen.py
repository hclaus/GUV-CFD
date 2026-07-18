import math

from guvcfd.mesh_gen import (
    _opening_box, opening_center, opening_half_extents, topo_set_dict, create_patch_dict, write_mesh_dicts,
)

_ROOM = (3.2, 4.8, 2.57)  # Lx, Ly, Lz


def test_opening_box_xmin_matches_original_hardcoded_formula():
    # Regression check: _opening_box's generalization to 6 walls must
    # reproduce the exact old xMin/xMax-only geometry bit-for-bit.
    Lx, Ly, Lz = _ROOM
    lo, hi = _opening_box("xMin", Lx, Ly, Lz, (0.5, 0.85), (0.3, 0.3), eps=1e-4)
    assert lo == (-1e-4, 0.5 * Ly - 0.15, 0.85 * Lz - 0.15)
    assert hi == (1e-4, 0.5 * Ly + 0.15, 0.85 * Lz + 0.15)


def test_opening_box_xmax_matches_original_hardcoded_formula():
    Lx, Ly, Lz = _ROOM
    lo, hi = _opening_box("xMax", Lx, Ly, Lz, (0.5, 0.15), (0.3, 0.3), eps=1e-4)
    assert lo == (Lx - 1e-4, 0.5 * Ly - 0.15, 0.15 * Lz - 0.15)
    assert hi == (Lx + 1e-4, 0.5 * Ly + 0.15, 0.15 * Lz + 0.15)


def test_opening_box_floor_and_ceiling_use_xy_in_plane():
    Lx, Ly, Lz = _ROOM
    lo, hi = _opening_box("floor", Lx, Ly, Lz, (0.5, 0.5), (0.4, 0.2), eps=1e-4)
    assert lo == (0.5 * Lx - 0.2, 0.5 * Ly - 0.1, -1e-4)
    assert hi == (0.5 * Lx + 0.2, 0.5 * Ly + 0.1, 1e-4)

    lo, hi = _opening_box("ceiling", Lx, Ly, Lz, (0.5, 0.5), (0.4, 0.2), eps=1e-4)
    assert lo == (0.5 * Lx - 0.2, 0.5 * Ly - 0.1, Lz - 1e-4)
    assert hi == (0.5 * Lx + 0.2, 0.5 * Ly + 0.1, Lz + 1e-4)


def test_opening_box_front_and_back_wall_use_xz_in_plane():
    Lx, Ly, Lz = _ROOM
    lo, hi = _opening_box("frontWall", Lx, Ly, Lz, (0.5, 0.5), (0.4, 0.2), eps=1e-4)
    assert lo == (0.5 * Lx - 0.2, -1e-4, 0.5 * Lz - 0.1)
    assert hi == (0.5 * Lx + 0.2, 1e-4, 0.5 * Lz + 0.1)

    lo, hi = _opening_box("backWall", Lx, Ly, Lz, (0.5, 0.5), (0.4, 0.2), eps=1e-4)
    assert lo == (0.5 * Lx - 0.2, Ly - 1e-4, 0.5 * Lz - 0.1)
    assert hi == (0.5 * Lx + 0.2, Ly + 1e-4, 0.5 * Lz + 0.1)


def test_opening_box_rejects_unknown_wall():
    import pytest
    with pytest.raises(ValueError, match="Unsupported wall"):
        _opening_box("ceilingFan", *_ROOM, (0.5, 0.5), (0.3, 0.3))


def test_topo_set_dict_without_second_openings():
    box = _opening_box("xMin", *_ROOM, (0.5, 0.5), (0.3, 0.3))
    text = topo_set_dict(box, box)
    assert text.count("boxToFace") == 2
    assert "inletFaces" in text and "outletFaces" in text
    assert "inlet2Faces" not in text and "outlet2Faces" not in text


def test_topo_set_dict_with_second_openings():
    box = _opening_box("xMin", *_ROOM, (0.5, 0.5), (0.3, 0.3))
    text = topo_set_dict(box, box, inlet2_box=box, outlet2_box=box)
    assert text.count("boxToFace") == 4
    for name in ("inletFaces", "outletFaces", "inlet2Faces", "outlet2Faces"):
        assert name in text


def test_create_patch_dict_flags_control_which_patches_appear():
    text = create_patch_dict()
    assert "name        inlet;" in text and "name        outlet;" in text
    assert "inlet2" not in text and "outlet2" not in text

    text2 = create_patch_dict(has_inlet2=True, has_outlet2=True)
    assert "name        inlet2;" in text2 and "name        outlet2;" in text2


def test_write_mesh_dicts_with_second_openings_on_different_walls(tmp_path):
    case_dir = tmp_path
    (case_dir / "system").mkdir()
    write_mesh_dicts(
        str(case_dir), *_ROOM,
        inlet_wall="xMin", inlet_center=(0.5, 0.85), inlet_size=(0.3, 0.3),
        outlet_wall="xMax", outlet_center=(0.5, 0.15), outlet_size=(0.3, 0.3),
        inlet2_wall="ceiling", inlet2_center=(0.5, 0.5), inlet2_size=(0.2, 0.2),
        outlet2_wall="floor", outlet2_center=(0.5, 0.5), outlet2_size=(0.2, 0.2),
    )
    topo_text = (case_dir / "system" / "topoSetDict").read_text()
    patch_text = (case_dir / "system" / "createPatchDict").read_text()
    for name in ("inlet", "outlet", "inlet2", "outlet2"):
        assert f"{name}Faces" in topo_text
        assert f"name        {name};" in patch_text


def test_opening_box_snaps_edges_to_grid_when_cell_size_given():
    # A 4x3m room's exact center (2.0, 1.5) sits on a mesh vertex when
    # cell_size=0.1 (both dims divide evenly) - a 0.3m-wide opening
    # centered there needs 3 cells, an odd count that can't straddle a
    # vertex symmetrically, so the raw (unsnapped) box edges land almost
    # exactly on a face-center grid line (1.85, 2.15) - a boxToFace
    # floating-point boundary tie that produces a lopsided carved patch.
    # Snapping should instead produce edges that are exact multiples of
    # cell_size, regardless of that parity mismatch.
    lo, hi = _opening_box("ceiling", 4.0, 3.0, 2.7, (0.5, 0.5), (0.3, 0.3), cell_size=0.1, eps=0.0)
    for v in (lo[0], hi[0], lo[1], hi[1]):
        # a multiple of 0.1, allowing for float roundoff
        assert abs(round(v / 0.1) * 0.1 - v) < 1e-9


def test_opening_box_snapping_never_collapses_to_zero_width():
    # A very small opening (smaller than one cell) must still snap to at
    # least one whole cell, not collapse to a zero-width (empty) box.
    lo, hi = _opening_box("ceiling", 4.0, 3.0, 2.7, (0.5, 0.5), (0.02, 0.02), cell_size=0.1, eps=0.0)
    assert hi[0] - lo[0] >= 0.1
    assert hi[1] - lo[1] >= 0.1


def test_opening_box_snapping_is_a_noop_when_already_grid_aligned():
    # An opening that already divides evenly (0.4m on a 0.1m grid, 4
    # cells - an even count, so it *can* straddle the vertex-centered
    # room center symmetrically) shouldn't be perturbed by snapping.
    lo_raw, hi_raw = _opening_box("ceiling", 4.0, 3.0, 2.7, (0.5, 0.5), (0.4, 0.4), eps=0.0)
    lo_snap, hi_snap = _opening_box("ceiling", 4.0, 3.0, 2.7, (0.5, 0.5), (0.4, 0.4), cell_size=0.1, eps=0.0)
    for a, b in zip(lo_raw, lo_snap):
        assert abs(a - b) < 1e-9
    for a, b in zip(hi_raw, hi_snap):
        assert abs(a - b) < 1e-9


def test_opening_center_uses_the_same_snapped_box_as_write_mesh_dicts():
    # opening_center() must reflect the *actual* carved geometry (same
    # cell_size passed to write_mesh_dicts), not the nominal/unsnapped
    # center - otherwise the ceiling-diffuser radial direction math would
    # be centered on a point that doesn't match the real patch. For this
    # room, the nominal center (2.0, 1.5) sits exactly on a mesh vertex,
    # and a 0.3m opening (3 cells - an odd, unstraddleable count) forces a
    # real half-cell shift in x once snapped, while y (2 cells - even)
    # doesn't need to shift.
    center_unsnapped = opening_center("ceiling", 4.0, 3.0, 2.7, (0.5, 0.5), (0.3, 0.3))
    center_snapped = opening_center("ceiling", 4.0, 3.0, 2.7, (0.5, 0.5), (0.3, 0.3), cell_size=0.1)
    lo, hi = _opening_box("ceiling", 4.0, 3.0, 2.7, (0.5, 0.5), (0.3, 0.3), cell_size=0.1, eps=0.0)
    expected = tuple((l + h) / 2 for l, h in zip(lo, hi))
    assert center_snapped[0] == expected[0] and center_snapped[1] == expected[1]
    assert center_unsnapped == (2.0, 1.5, 2.7)
    assert abs(abs(center_snapped[0] - 2.0) - 0.05) < 1e-9  # shifted by half a cell in x
    assert abs(center_snapped[1] - 1.5) < 1e-9  # y needed no shift (even cell count)


def test_opening_half_extents_matches_nominal_size_when_already_grid_aligned():
    hw, hh = opening_half_extents("ceiling", 4.0, 3.0, 2.7, (0.5, 0.5), (0.4, 0.4), cell_size=0.1)
    assert math.isclose(hw, 0.2, abs_tol=1e-9)
    assert math.isclose(hh, 0.2, abs_tol=1e-9)


def test_opening_half_extents_reflects_the_same_snapped_box_as_opening_center():
    # 0.6 x 0.3 opening on xMax (the real project's failing-then-fixed
    # geometry) - half-extents should be the TRUE physical half-width/
    # half-height of whatever box actually got carved, matching
    # _opening_box exactly (not just the nominal size/2).
    lo, hi = _opening_box("xMax", 4.0, 3.0, 2.7, (0.3, 0.8), (0.6, 0.3), cell_size=0.1, eps=0.0)
    hw, hh = opening_half_extents("xMax", 4.0, 3.0, 2.7, (0.3, 0.8), (0.6, 0.3), cell_size=0.1)
    # xMax's in-plane axes are (a1=1/y, a2=2/z) - see _WALL_SPECS.
    assert math.isclose(hw, (hi[1] - lo[1]) / 2, abs_tol=1e-9)
    assert math.isclose(hh, (hi[2] - lo[2]) / 2, abs_tol=1e-9)


def test_opening_half_extents_no_snap_matches_nominal_size_exactly():
    hw, hh = opening_half_extents("frontWall", 4.0, 3.0, 2.7, (0.5, 0.5), (0.5, 0.2))
    assert math.isclose(hw, 0.25, abs_tol=1e-9)
    assert math.isclose(hh, 0.1, abs_tol=1e-9)
