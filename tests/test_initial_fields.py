import math

import pytest

from guvcfd import initial_fields
from guvcfd.initial_fields import (
    compute_inlet_velocity, compute_inlet_velocities, WALL_INFLOW_DIRECTION,
    boundary_field_block, field_file_content,
    compute_radial_inlet_velocities, resolve_inlet_velocity,
)

_ROOM_VOLUME = 3.2 * 4.8 * 2.57


def test_wall_inflow_direction_covers_all_six_walls_as_unit_vectors():
    walls = {"xMin", "xMax", "frontWall", "backWall", "floor", "ceiling"}
    assert set(WALL_INFLOW_DIRECTION) == walls
    for wall, d in WALL_INFLOW_DIRECTION.items():
        assert sum(abs(c) for c in d) == 1  # a single-axis unit vector


def test_compute_inlet_velocities_single_opening_matches_compute_inlet_velocity():
    v_mag = compute_inlet_velocity(6.0, _ROOM_VOLUME, 0.08)
    [v] = compute_inlet_velocities(6.0, _ROOM_VOLUME, [("xMax", 0.08)])
    assert v == tuple(v_mag * d for d in WALL_INFLOW_DIRECTION["xMax"])


def test_compute_inlet_velocities_splits_by_area_and_conserves_total_flow():
    # Two inlets, one twice the area of the other - same velocity
    # magnitude at each (so the larger one carries twice the flow), and
    # the combined flow rate must still equal the ACH-derived total.
    ach, area1, area2 = 6.0, 0.08, 0.16
    v1, v2 = compute_inlet_velocities(ach, _ROOM_VOLUME, [("xMin", area1), ("xMax", area2)])
    mag1 = sum(c ** 2 for c in v1) ** 0.5
    mag2 = sum(c ** 2 for c in v2) ** 0.5
    assert abs(mag1 - mag2) < 1e-9  # same magnitude at each inlet

    total_flow = mag1 * area1 + mag2 * area2
    target_flow = ach * _ROOM_VOLUME / 3600.0
    assert abs(total_flow - target_flow) < 1e-9


def test_compute_inlet_velocities_points_along_each_openings_own_wall():
    [v_ceiling] = compute_inlet_velocities(6.0, _ROOM_VOLUME, [("ceiling", 0.1)])
    assert v_ceiling[0] == 0 and v_ceiling[1] == 0 and v_ceiling[2] < 0  # inward = downward

    [v_floor] = compute_inlet_velocities(6.0, _ROOM_VOLUME, [("floor", 0.1)])
    assert v_floor[2] > 0  # inward = upward


def test_boundary_field_block_omits_second_openings_by_default():
    text = boundary_field_block("U", inlet_velocity=(0.5, 0, 0))
    assert "    inlet2\n" not in text
    assert "    outlet2\n" not in text


def test_boundary_field_block_includes_inlet2_with_its_own_velocity():
    text = boundary_field_block("U", inlet_velocity=(0.5, 0, 0), inlet2_velocity=(0, 0, -0.25))
    assert "inlet2" in text
    assert "uniform (0.5 0 0)" in text
    assert "uniform (0 0 -0.25)" in text


def test_boundary_field_block_outlet2_mirrors_primary_outlet_for_non_U_fields():
    # p's outlet BC (fixedValue uniform 0) is a fixed constant regardless
    # of instance - outlet2 should get the identical block.
    text = boundary_field_block("p", has_outlet2=True)
    assert "    outlet2\n    {\n            type            fixedValue;\n            value           uniform 0;\n    }" in text


def test_field_file_content_with_both_second_openings_stays_valid_dict_shape():
    content = field_file_content(
        "U", inlet_velocity=(0.5, 0, 0), inlet2_velocity=(0, 0, -0.3), has_outlet2=True,
    )
    assert content.count("{") == content.count("}")
    for patch in ("inlet", "inlet2", "outlet", "outlet2"):
        assert f"    {patch}\n    {{" in content


def test_radial_velocities_preserve_magnitude_and_spread_apart():
    # A ceiling opening centered at (2, 1.5, 2.7), half_extents=(0.3,0.3),
    # 3 faces exactly at that half-extent (r_norm=1 - full surface_angle_deg
    # tilt, no tapering effect to account for here).
    opening_center = (2.0, 1.5, 2.7)
    half_extents = (0.3, 0.3)
    face_centers = [
        (2.3, 1.5, 2.7),   # +x of center
        (2.0, 1.8, 2.7),   # +y of center
        (1.7, 1.5, 2.7),   # -x of center
    ]
    v_mag = 0.5
    velocities = compute_radial_inlet_velocities(face_centers, opening_center, "ceiling", v_mag, half_extents)

    assert len(velocities) == 3
    for v in velocities:
        assert math.isclose(math.sqrt(sum(c * c for c in v)), v_mag, rel_tol=1e-9)

    # All 3 faces are at the opening's half-extent -> all at "the edge"
    # (r_norm=1) -> every one gets the same, full surface_angle_deg tilt.
    for v in velocities:
        assert v[2] < 0
        assert math.isclose(v[2], -v_mag * math.sin(math.radians(15)), rel_tol=1e-6)

    # The 3 directions should be mutually distinct (spread apart), not
    # collapsed onto the same or opposite vectors.
    for i in range(3):
        for j in range(i + 1, 3):
            diff = math.sqrt(sum((a - b) ** 2 for a, b in zip(velocities[i], velocities[j])))
            assert diff > 1e-6


def test_radial_velocities_generalize_to_a_side_wall():
    # xMin's in-plane axes are (y,z), inward normal (1,0,0). A single face
    # exactly at its declared half-extent (r_norm=1, full tilt).
    opening_center = (0.0, 1.5, 1.2)
    half_extents = (0.3, 0.3)
    face_centers = [(0.0, 1.8, 1.2)]
    v_mag = 0.3
    v = compute_radial_inlet_velocities(face_centers, opening_center, "xMin", v_mag, half_extents)[0]
    assert math.isclose(math.sqrt(sum(c * c for c in v)), v_mag, rel_tol=1e-9)
    assert v[0] > 0  # tilted into the room along xMin's inward normal (+x)
    assert math.isclose(v[0], v_mag * math.sin(math.radians(15)), rel_tol=1e-6)


def test_radial_velocities_taper_toward_center_angle_near_the_opening_center():
    # A face near the opening's center should get mostly the center_angle
    # (straight into the room, little radial spread); a face at the
    # opening's outer edge should get mostly surface_angle (strong radial
    # spread) - this is the fix for the near-center singularity (see
    # docstring): two faces immediately straddling the center no longer
    # differ by anywhere near full magnitude, because neither pushes hard
    # radially right there.
    opening_center = (2.0, 1.5, 2.7)
    half_extents = (0.3, 0.3)
    near_center = (2.02, 1.5, 2.7)   # r=0.02 -> r_norm=0.0667
    at_edge = (2.3, 1.5, 2.7)        # r=0.3 -> r_norm=1 (at the true edge)
    v_mag = 0.5
    v_near, v_edge = compute_radial_inlet_velocities(
        [near_center, at_edge], opening_center, "ceiling", v_mag, half_extents)

    # normal (z, tilt) component: near-center face should be much closer
    # to center_angle_deg=90 (i.e. |z| close to v_mag), edge face close to
    # surface_angle_deg=15 (i.e. |z| close to v_mag*sin(15)).
    assert abs(v_near[2]) > abs(v_edge[2])
    assert math.isclose(v_edge[2], -v_mag * math.sin(math.radians(15)), rel_tol=1e-6)

    # in-plane (radial) component: near-center face should be much smaller
    # than the edge face's.
    in_plane_near = math.sqrt(v_near[0] ** 2 + v_near[1] ** 2)
    in_plane_edge = math.sqrt(v_edge[0] ** 2 + v_edge[1] ** 2)
    assert in_plane_near < in_plane_edge

    # Both still preserve exact magnitude v_mag regardless of tilt blend.
    assert math.isclose(math.sqrt(sum(c * c for c in v_near)), v_mag, rel_tol=1e-9)
    assert math.isclose(math.sqrt(sum(c * c for c in v_edge)), v_mag, rel_tol=1e-9)


def test_radial_velocities_cover_full_circle_uniformly():
    # 8 faces evenly spaced around a circle, opening's half_extents equal
    # in both axes (a "square" opening) - a uniform scale factor doesn't
    # distort angles, so an already-uniform circular input stays uniform.
    opening_center = (2.0, 1.5, 2.7)
    half_extents = (0.3, 0.3)
    r = 0.3
    face_centers = [
        (2.0 + r * math.cos(math.radians(a)), 1.5 + r * math.sin(math.radians(a)), 2.7)
        for a in range(0, 360, 45)
    ]
    v_mag = 0.4
    velocities = compute_radial_inlet_velocities(face_centers, opening_center, "ceiling", v_mag, half_extents)
    angles = sorted(math.degrees(math.atan2(v[1], v[0])) % 360 for v in velocities)
    gaps = [(angles[(i + 1) % 8] - angles[i]) % 360 for i in range(8)]
    for gap in gaps:
        assert math.isclose(gap, 45.0, abs_tol=1e-6)


def test_radial_velocities_any_midline_face_is_exactly_cardinal():
    # The shape-normalized angle formula (docstring point 2) means ANY
    # face on the opening's actual midline gets an exact cardinal
    # direction - not just one arbitrarily-chosen face, unlike the
    # discrete "sort and redistribute" scheme this replaced. Elongated,
    # non-square opening (0.6 x 0.3) so hw != hh matters.
    opening_center = (2.0, 1.5, 2.7)
    half_extents = (0.3, 0.15)
    # 3 different faces, all on the z=const (dz=0) midline, at different
    # distances from center - every one should point purely in +/-x.
    face_centers = [(2.05, 1.5, 2.7), (2.15, 1.5, 2.7), (1.9, 1.5, 2.7)]
    v_mag = 0.4
    velocities = compute_radial_inlet_velocities(face_centers, opening_center, "ceiling", v_mag, half_extents)
    for fc, v in zip(face_centers, velocities):
        in_plane_y = v[1]
        assert math.isclose(in_plane_y, 0.0, abs_tol=1e-9)
        expected_sign = 1 if fc[0] > opening_center[0] else -1
        assert (v[0] > 0) == (expected_sign > 0)


def test_radial_velocities_adjacent_grid_faces_no_longer_flip_near_180_degrees():
    # Regression test for the real failure this fix addresses: on an
    # even-width rectangular grid, two faces immediately straddling the
    # opening's exact center used to get assigned near-opposite
    # directions (a velocity discontinuity that destabilized a real
    # steady-state UV-decay solve). Reproduces the failing project's
    # actual opening size (0.6 x 0.3 m at cell_size=0.1 m -> 6x3 grid),
    # using the opening's TRUE physical half-extents (0.3, 0.15) - not the
    # mesh's own sampled face-position extremes, which under-reach the
    # true edge by half a cell.
    cell = 0.1
    w, h = 0.6, 0.3
    cy, cz = 1.2, 2.4
    half_extents = (w / 2, h / 2)
    ys = [cy - w / 2 + cell / 2 + i * cell for i in range(6)]
    zs = [cz - h / 2 + cell / 2 + i * cell for i in range(3)]
    face_centers = [(0.0, y, z) for z in zs for y in ys]
    v_mag = 0.278

    velocities = compute_radial_inlet_velocities(face_centers, (0.0, cy, cz), "xMax", v_mag, half_extents)

    n_y, n_z = 6, 3
    max_diff = 0.0
    for iz in range(n_z):
        for iy in range(n_y):
            idx = iz * n_y + iy
            for diy, diz in ((1, 0), (0, 1)):
                jy, jz = iy + diy, iz + diz
                if jy < n_y and jz < n_z:
                    jdx = jz * n_y + jy
                    diff = math.sqrt(sum((a - b) ** 2 for a, b in zip(velocities[idx], velocities[jdx])))
                    max_diff = max(max_diff, diff)

    # Old (pre-fix) model's worst adjacent-face jump on this exact opening
    # was 0.537 (out of a theoretical max of 2*v_mag=0.556 - i.e. almost a
    # full 180 degree flip). This fix (true-extent Euclidean taper +
    # shape-normalized angle) measured 0.233 - verify it stays well below
    # the old value, with margin for float noise.
    assert max_diff < 0.3


def test_radial_velocity_falls_back_to_normal_when_face_is_at_center():
    # A degenerate case: face center coincides exactly with the opening
    # center - no radial direction is defined, must not divide by zero.
    v = compute_radial_inlet_velocities([(1.0, 1.0, 1.0)], (1.0, 1.0, 1.0), "floor", 0.4, (0.3, 0.3))[0]
    assert v == pytest.approx(tuple(0.4 * d for d in WALL_INFLOW_DIRECTION["floor"]))


def test_radial_velocities_return_plain_floats_not_numpy():
    # Must be JSON-serializable once stored in results.json/summary dicts.
    v = compute_radial_inlet_velocities([(2.3, 1.5, 2.7)], (2.0, 1.5, 2.7), "ceiling", 0.5, (0.3, 0.3))[0]
    assert all(isinstance(c, float) for c in v)


def test_resolve_direct_returns_plain_tuple_unchanged():
    result = resolve_inlet_velocity("unused", "inlet", "xMin", (0, 1.5, 1.2), 0.278, diffuser_type="direct")
    assert result == tuple(0.278 * d for d in WALL_INFLOW_DIRECTION["xMin"])


def test_resolve_ceiling_reads_face_centers_and_computes_radial(monkeypatch):
    calls = []

    def fake_read_patch_face_centers(case_dir, patch_name):
        calls.append((case_dir, patch_name))
        return [(0.3, 1.5, 2.7), (-0.3, 1.5, 2.7)]

    monkeypatch.setattr(initial_fields, "read_patch_face_centers", fake_read_patch_face_centers)
    result = resolve_inlet_velocity("case123", "inlet", "ceiling", (0, 1.5, 2.7), 0.5, diffuser_type="ceiling",
                                     half_extents=(0.3, 0.3))

    assert calls == [("case123", "inlet")]
    assert len(result) == 2
    for v in result:
        assert math.isclose(math.sqrt(sum(c * c for c in v)), 0.5, rel_tol=1e-9)


def test_resolve_rejects_unknown_diffuser_type():
    with pytest.raises(ValueError, match="swirl"):
        resolve_inlet_velocity("case", "inlet", "xMin", (0, 0, 0), 0.5, diffuser_type="swirl")


def test_boundary_field_block_uniform_regression_unchanged():
    text = boundary_field_block("U", inlet_velocity=(0.278, 0, 0))
    assert "uniform (0.278 0 0)" in text
    assert "nonuniform" not in text


def test_boundary_field_block_accepts_per_face_velocity_list():
    per_face = [(0.1, 0.2, 0.3), (0.4, 0.5, 0.6), (-0.1, -0.2, -0.3)]
    text = boundary_field_block("U", inlet_velocity=per_face)
    assert "nonuniform List<vector>" in text
    assert "3" in text  # face count
    assert "(0.1 0.2 0.3)" in text
    assert "(-0.1 -0.2 -0.3)" in text


def test_boundary_field_block_inlet2_also_supports_per_face_list():
    per_face = [(0.1, 0.2, 0.3), (0.4, 0.5, 0.6)]
    text = boundary_field_block("U", inlet_velocity=(0.278, 0, 0), inlet2_velocity=per_face)
    assert "uniform (0.278 0 0)" in text  # primary inlet unaffected
    assert "nonuniform List<vector>" in text  # inlet2 gets the list
