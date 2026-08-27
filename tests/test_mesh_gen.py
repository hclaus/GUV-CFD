import math

from guvcfd.mesh_gen import (
    _opening_box, opening_center, opening_half_extents, topo_set_dict, create_patch_dict, write_mesh_dicts,
    suggest_opening_size_fix, suggest_opening_center_fix, _WALL_SPECS,
    lamp_refine_topo_set_dict, opening_refine_topo_set_dict, refine_mesh_dict, _opening_refine_box,
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


def test_create_patch_dict_sealed_closes_openings_as_walls():
    text = create_patch_dict(sealed=True)
    assert "type wall;" in text
    assert "type patch;" not in text

    text2 = create_patch_dict(has_inlet2=True, has_outlet2=True, sealed=True)
    assert text2.count("type wall;") == 4
    assert "type patch;" not in text2

    # sealed=False (the default) is untouched.
    assert "type patch;" in create_patch_dict()


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


def test_opening_box_snapping_never_shrinks_below_the_requested_size():
    # The core invariant the outward-snap fix guarantees: the carved
    # opening always CONTAINS the nominal (unsnapped) box, on every wall
    # and axis, regardless of tie parity - never smaller than requested.
    # Swept across a range of centers/sizes that land on ties, near-ties,
    # and clean multiples alike.
    for center in [(0.5, 0.5), (0.3, 0.7), (0.1, 0.9), (0.5, 0.1)]:
        for size in [(0.3, 0.3), (0.1, 0.2), (0.25, 0.15), (0.4, 0.4)]:
            lo_raw, hi_raw = _opening_box("ceiling", 4.0, 3.0, 2.7, center, size, eps=0.0)
            lo_snap, hi_snap = _opening_box("ceiling", 4.0, 3.0, 2.7, center, size, cell_size=0.1, eps=0.0)
            assert lo_snap[0] <= lo_raw[0] + 1e-9, (center, size)
            assert hi_snap[0] >= hi_raw[0] - 1e-9, (center, size)
            assert lo_snap[1] <= lo_raw[1] + 1e-9, (center, size)
            assert hi_snap[1] >= hi_raw[1] - 1e-9, (center, size)


def test_opening_box_matches_real_patient_ward_case_that_used_to_shrink():
    # Regression test for the real bug: this exact combination (patient
    # ward room, 0.3x0.3m inlet centered at y=1.5/z=2.1, 0.1m mesh) used
    # to silently snap DOWN to a 0.2x0.2m opening (44% of the requested
    # area) under round-to-nearest, because both edges landed exactly on
    # a rounding tie on both axes - confirmed directly against the real
    # CFD mesh this produced (0.0395 m^2 actual vs 0.09 m^2 nominal). The
    # outward-snap fix must not reproduce that shrinkage.
    Lx, Ly, Lz = 3.2, 4.8, 2.57
    center_frac = (1.5 / Ly, 2.1 / Lz)
    lo, hi = _opening_box("xMin", Lx, Ly, Lz, center_frac, (0.3, 0.3), cell_size=0.1, eps=0.0)
    width, height = hi[1] - lo[1], hi[2] - lo[2]
    assert width >= 0.3 - 1e-9
    assert height >= 0.3 - 1e-9


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


def test_opening_box_snaps_to_the_actual_mesh_grid_not_the_nominal_cell_size():
    # Regression test for a real, confirmed bug (2026-08-19): _opening_box
    # used to snap against the raw nominal cell_size, but block_mesh_dict
    # builds n = round(length/cell_size) cells spanning the room's EXACT
    # dimension - whenever length/cell_size isn't a whole number, that
    # produces an ACTUAL per-axis cell size different from the nominal
    # one (e.g. a 5m room depth at nominal 0.09m builds 56 cells of
    # 0.089286m each). Snapping against the wrong (nominal) grid landed
    # carved opening edges up to ~28mm off every real mesh grid line,
    # silently reintroducing the exact boxToFace floating-point boundary-
    # tie problem this snapping exists to prevent. Confirmed directly on
    # this exact room/opening/cell_size combination before the fix.
    Lx, Ly, Lz = 4.0, 5.0, 3.0
    cell_size = 0.09
    actual_dy = Ly / round(Ly / cell_size)  # 5.0 / 56 = 0.089285714...
    actual_dz = Lz / round(Lz / cell_size)  # 3.0 / 33 = 0.090909090...
    lo, hi = _opening_box("xMin", Lx, Ly, Lz, (2.5 / Ly, 2.55 / Lz), (0.4, 0.4),
                           cell_size=cell_size, eps=0.0)
    for v in (lo[1], hi[1]):
        assert abs(round(v / actual_dy) * actual_dy - v) < 1e-9
    for v in (lo[2], hi[2]):
        assert abs(round(v / actual_dz) * actual_dz - v) < 1e-9
    # And explicitly NOT aligned to the nominal cell_size grid instead -
    # pins the fix against silently reverting to the old (wrong) behavior.
    assert abs(round(lo[1] / cell_size) * cell_size - lo[1]) > 1e-3


def test_opening_center_uses_the_same_snapped_box_as_write_mesh_dicts():
    # opening_center() must reflect the *actual* carved geometry (same
    # cell_size passed to write_mesh_dicts), not the nominal/unsnapped
    # center - otherwise the ceiling-diffuser radial direction math would
    # be centered on a point that doesn't match the real patch. For this
    # room, the nominal center (2.0, 1.5) sits exactly on a mesh vertex,
    # and a 0.3m opening (3 cells - an odd, unstraddleable count) puts
    # BOTH edges exactly on a snap tie on both axes - snapping outward
    # (floor the low edge, ceil the high edge) expands each tied edge by
    # exactly half a cell in opposite directions, so the center itself
    # doesn't move at all (0.3m grows to 0.4m, symmetrically, on both
    # axes) - unlike round-to-nearest, which can shift the center and/or
    # shrink the opening depending on incidental tie parity.
    center_unsnapped = opening_center("ceiling", 4.0, 3.0, 2.7, (0.5, 0.5), (0.3, 0.3))
    center_snapped = opening_center("ceiling", 4.0, 3.0, 2.7, (0.5, 0.5), (0.3, 0.3), cell_size=0.1)
    lo, hi = _opening_box("ceiling", 4.0, 3.0, 2.7, (0.5, 0.5), (0.3, 0.3), cell_size=0.1, eps=0.0)
    expected = tuple((l + h) / 2 for l, h in zip(lo, hi))
    assert center_snapped[0] == expected[0] and center_snapped[1] == expected[1]
    assert center_unsnapped == (2.0, 1.5, 2.7)
    assert abs(center_snapped[0] - 2.0) < 1e-9  # no shift - outward snap grows symmetrically
    assert abs(center_snapped[1] - 1.5) < 1e-9  # no shift - outward snap grows symmetrically
    assert abs((hi[0] - lo[0]) - 0.4) < 1e-9  # grew from 0.3m to 0.4m (one full cell, split evenly)
    assert abs((hi[1] - lo[1]) - 0.4) < 1e-9


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


# --- suggest_opening_size_fix / suggest_opening_center_fix (2026-08-07) ---

def test_suggest_opening_size_fix_is_a_noop_when_already_exact_multiples():
    w, h = suggest_opening_size_fix("xMin", 4.0, 5.0, 3.0, (0.4, 0.3), 0.1)
    assert math.isclose(w, 0.4, abs_tol=1e-9)
    assert math.isclose(h, 0.3, abs_tol=1e-9)


def test_suggest_opening_size_fix_stays_a_noop_under_fp_noise():
    # 0.1*3 is 0.30000000000000004 in binary float - must not spuriously
    # bump to 0.4 just because of that representation noise.
    noisy = 0.1 + 0.1 + 0.1
    assert noisy != 0.3  # sanity: confirms this IS the noisy case
    w, h = suggest_opening_size_fix("xMin", 4.0, 5.0, 3.0, (noisy, 0.4), 0.1)
    assert math.isclose(w, 0.3, abs_tol=1e-9)
    assert math.isclose(h, 0.4, abs_tol=1e-9)


def test_suggest_opening_size_fix_rounds_up_only_the_off_axis():
    w, h = suggest_opening_size_fix("xMin", 4.0, 5.0, 3.0, (0.37, 0.4), 0.1)
    assert math.isclose(w, 0.4, abs_tol=1e-9)
    assert math.isclose(h, 0.4, abs_tol=1e-9)


def test_suggest_opening_size_fix_rounds_up_both_axes():
    w, h = suggest_opening_size_fix("xMin", 4.0, 5.0, 3.0, (0.23, 0.35), 0.1)
    assert math.isclose(w, 0.3, abs_tol=1e-9)
    assert math.isclose(h, 0.4, abs_tol=1e-9)


def test_suggest_opening_size_and_center_fix_use_the_actual_not_nominal_grid():
    # Regression test for a real, confirmed bug (2026-08-19, same root
    # cause as _opening_box's identical bug): both functions used to snap
    # against the raw nominal cell_size, but block_mesh_dict builds
    # n = round(length/cell_size) cells spanning the room's EXACT
    # dimension - whenever length/cell_size isn't a whole number (0.09m
    # on this room, unlike the other tests' 0.1m which divides evenly),
    # the suggested "fix" landed on the wrong (nominal) grid instead of
    # the real one.
    Lx, Ly, Lz = 4.0, 5.0, 3.0
    cell_size = 0.09
    actual_dy = Ly / round(Ly / cell_size)
    actual_dz = Lz / round(Lz / cell_size)

    sw, sh = suggest_opening_size_fix("xMin", Lx, Ly, Lz, (0.4, 0.4), cell_size)
    assert abs(round(sw / actual_dy) * actual_dy - sw) < 1e-9
    assert abs(round(sh / actual_dz) * actual_dz - sh) < 1e-9
    # not aligned to the nominal grid instead
    assert abs(round(sw / cell_size) * cell_size - sw) > 1e-3

    (_, _), (sug_y, sug_z) = suggest_opening_center_fix(
        "xMin", Lx, Ly, Lz, (2.5 / Ly, 2.5 / Lz), (sw, sh), cell_size)
    assert abs(round((sug_y - sw / 2) / actual_dy) * actual_dy - (sug_y - sw / 2)) < 1e-9
    assert abs(round((sug_z - sh / 2) / actual_dz) * actual_dz - (sug_z - sh / 2)) < 1e-9


def test_suggest_opening_center_fix_even_width_aligns_to_grid_lines():
    # 0.4m width / 0.1m cells = 4 (even) -> valid centers are exact grid
    # lines. A center already on one needs no shift.
    (cur_y, cur_z), (sug_y, sug_z) = suggest_opening_center_fix(
        "xMin", 4.0, 5.0, 3.0, (2.5 / 5.0, 0.4 / 3.0), (0.4, 0.4), 0.1)
    assert math.isclose(cur_y, 2.5, abs_tol=1e-9) and math.isclose(sug_y, 2.5, abs_tol=1e-9)
    assert math.isclose(cur_z, 0.4, abs_tol=1e-9) and math.isclose(sug_z, 0.4, abs_tol=1e-9)


def test_suggest_opening_center_fix_even_width_off_grid_shifts_to_nearest_line():
    (cur_y, _), (sug_y, _) = suggest_opening_center_fix(
        "xMin", 4.0, 5.0, 3.0, (2.53 / 5.0, 0.4 / 3.0), (0.4, 0.4), 0.1)
    assert math.isclose(cur_y, 2.53, abs_tol=1e-9)
    assert math.isclose(sug_y, 2.5, abs_tol=1e-9)  # nearest grid line, not 2.6


def test_suggest_opening_center_fix_odd_width_needs_cell_center_offset_not_grid_line():
    # 0.3m width / 0.1m cells = 3 (odd) -> valid centers sit at
    # cell-CENTER offsets (..., 0.05, 0.15, 0.25, ...), NOT grid lines.
    # A center sitting exactly ON a grid line (2.5) is the "naive wrong
    # assumption" case - must still shift by half a cell.
    (cur_y, _), (sug_y, _) = suggest_opening_center_fix(
        "xMin", 4.0, 5.0, 3.0, (2.5 / 5.0, 0.4 / 3.0), (0.3, 0.4), 0.1)
    assert math.isclose(cur_y, 2.5, abs_tol=1e-9)
    assert math.isclose(sug_y, 2.45, abs_tol=1e-9) or math.isclose(sug_y, 2.55, abs_tol=1e-9)
    assert abs(sug_y - cur_y) - 0.05 < 1e-9  # exactly half a cell, the max possible


def test_suggest_opening_center_fix_never_shifts_more_than_half_a_cell():
    import random
    random.seed(0)
    for _ in range(200):
        w = round(random.uniform(0.2, 0.6), 1)  # already an exact multiple of cell_size
        y = random.uniform(0.5, 4.5)
        (cur_y, _), (sug_y, _) = suggest_opening_center_fix(
            "xMin", 4.0, 5.0, 3.0, (y / 5.0, 0.4 / 3.0), (w, 0.4), 0.1)
        assert abs(sug_y - cur_y) <= 0.05 + 1e-9, (w, y, cur_y, sug_y)


def test_suggest_opening_center_fix_breaks_exact_tie_toward_wall_center():
    # y=2.55 is exactly equidistant (0.05m) from both 2.5 and 2.6 for a
    # 0.4m (even) width. Wall midpoint on the y axis is Ly/2 = 2.5 - so
    # the tie must break toward 2.5, NOT toward 2.6 (which is what
    # Python's round()/banker's-rounding-style "round half to even" could
    # plausibly pick instead - this is the regression the tie-break logic
    # exists to prevent, mirroring snap_outward's own documented edge-tie
    # bug class).
    (cur_y, _), (sug_y, _) = suggest_opening_center_fix(
        "xMin", 4.0, 5.0, 3.0, (2.55 / 5.0, 0.4 / 3.0), (0.4, 0.4), 0.1)
    assert math.isclose(cur_y, 2.55, abs_tol=1e-9)
    assert math.isclose(sug_y, 2.5, abs_tol=1e-9)


def test_suggest_opening_center_fix_returns_absolute_meters_not_center_frac():
    (cur_y, cur_z), _ = suggest_opening_center_fix(
        "xMin", 4.0, 5.0, 3.0, (0.5, 0.5), (0.4, 0.4), 0.1)
    assert math.isclose(cur_y, 2.5, abs_tol=1e-9)   # 0.5 * Ly=5, NOT 0.5 itself
    assert math.isclose(cur_z, 1.5, abs_tol=1e-9)   # 0.5 * Lz=3, NOT 0.5 itself


def test_suggest_opening_center_fix_works_on_every_wall():
    # Sweep all 6 _WALL_SPECS entries to confirm a1/a2 axis selection and
    # dims indexing stay correct everywhere, not just the historically-
    # tested xMin/xMax.
    Lx, Ly, Lz = 4.0, 5.0, 3.0
    dims = (Lx, Ly, Lz)
    for wall, (_, _, (a1, a2)) in _WALL_SPECS.items():
        c1, c2 = 0.53 / dims[a1], 0.5 / dims[a2]  # off-grid on axis1, on-grid on axis2
        (cur1, cur2), (sug1, sug2) = suggest_opening_center_fix(wall, Lx, Ly, Lz, (c1, c2), (0.4, 0.4), 0.1)
        assert math.isclose(cur1, 0.53, abs_tol=1e-9), wall
        assert math.isclose(sug1, 0.5, abs_tol=1e-9), wall  # nearest grid line
        assert math.isclose(cur2, 0.5, abs_tol=1e-9), wall
        assert math.isclose(sug2, 0.5, abs_tol=1e-9), wall  # already aligned, no shift


def test_size_then_center_fix_matches_the_planning_session_worked_examples():
    # The 4 worked examples verified during planning, as an end-to-end
    # regression pin - room 4x5x3m, cell_size 0.1m, wall xMin.
    Lx, Ly, Lz = 4.0, 5.0, 3.0
    cases = [
        (0.23, 0.35, 1.37, 0.68, 0.3, 0.4, 1.35, 0.7),
        (0.40, 0.20, 3.62, 2.14, 0.4, 0.2, 3.6, 2.1),
        (0.31, 0.31, 2.05, 1.50, 0.4, 0.4, 2.1, 1.5),
        (0.27, 0.40, 0.45, 2.77, 0.3, 0.4, 0.45, 2.8),
    ]
    for w, h, y, z, exp_w, exp_h, exp_y, exp_z in cases:
        sw, sh = suggest_opening_size_fix("xMin", Lx, Ly, Lz, (w, h), 0.1)
        assert math.isclose(sw, exp_w, abs_tol=1e-9) and math.isclose(sh, exp_h, abs_tol=1e-9)
        (_, _), (sug_y, sug_z) = suggest_opening_center_fix(
            "xMin", Lx, Ly, Lz, (y / Ly, z / Lz), (sw, sh), 0.1)
        assert math.isclose(sug_y, exp_y, abs_tol=1e-9)
        assert math.isclose(sug_z, exp_z, abs_tol=1e-9)


def test_lamp_refine_topo_set_dict_unions_one_sphere_per_lamp():
    text = lamp_refine_topo_set_dict([(3.0, 0.2, 2.47), (0.2, 4.6, 2.47)], radius=0.4)
    assert text.count("sphereToCell") == 2
    assert text.count("lampRefineCells") == 2
    assert "action  new;" in text and "action  add;" in text
    assert "origin  (3 0.2 2.47);" in text
    assert "radius  0.4;" in text


def test_lamp_refine_topo_set_dict_single_lamp_is_all_new_no_add():
    text = lamp_refine_topo_set_dict([(1.0, 1.0, 1.0)], radius=0.3)
    assert text.count("sphereToCell") == 1
    assert "action  new;" in text
    assert "action  add;" not in text


def test_opening_refine_box_extends_inward_and_pads_in_plane():
    Lx, Ly, Lz = _ROOM
    lo, hi = _opening_refine_box("xMin", Lx, Ly, Lz, (0.5, 0.85), (0.3, 0.3), depth=0.3)
    # normal axis (x): a "low" wall (pos=0) extends INTO the room, 0 -> depth
    assert lo[0] == 0.0 and hi[0] == 0.3
    # in-plane axes padded by depth on both sides beyond the opening's own half-size
    assert math.isclose(lo[1], 0.5 * Ly - 0.15 - 0.3)
    assert math.isclose(hi[1], 0.5 * Ly + 0.15 + 0.3)
    assert math.isclose(lo[2], 0.85 * Lz - 0.15 - 0.3)
    assert math.isclose(hi[2], 0.85 * Lz + 0.15 + 0.3)


def test_opening_refine_box_high_wall_extends_inward_the_other_direction():
    Lx, Ly, Lz = _ROOM
    lo, hi = _opening_refine_box("xMax", Lx, Ly, Lz, (0.5, 0.15), (0.3, 0.3), depth=0.3)
    # a "high" wall (pos=Lx) extends INTO the room too, i.e. Lx-depth -> Lx
    assert math.isclose(lo[0], Lx - 0.3) and math.isclose(hi[0], Lx)


def test_opening_refine_topo_set_dict_unions_one_box_per_opening():
    Lx, Ly, Lz = _ROOM
    inlet_box = _opening_refine_box("xMin", Lx, Ly, Lz, (0.5, 0.85), (0.3, 0.3), depth=0.3)
    outlet_box = _opening_refine_box("xMax", Lx, Ly, Lz, (0.5, 0.15), (0.3, 0.3), depth=0.3)
    text = opening_refine_topo_set_dict([inlet_box, outlet_box])
    assert text.count("boxToCell") == 2
    assert text.count("openingRefineCells") == 2
    assert "action  new;" in text and "action  add;" in text


def test_refine_mesh_dict_uses_hex_topology_not_geometric_cut():
    text = refine_mesh_dict("lampRefineCells")
    assert "set             lampRefineCells;" in text
    assert "useHexTopology  true;" in text
    assert "geometricCut    false;" in text
    assert "tan1" in text and "tan2" in text and "normal" in text
    assert "coordinateSystem global;" in text
    assert "globalCoeffs" in text
