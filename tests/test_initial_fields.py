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


def test_radial_velocities_point_outward_and_preserve_magnitude():
    # A ceiling opening centered at (2, 1.5, 2.7), faces spread around it.
    opening_center = (2.0, 1.5, 2.7)
    face_centers = [
        (2.3, 1.5, 2.7),   # +x of center
        (2.0, 1.8, 2.7),   # +y of center
        (1.7, 1.5, 2.7),   # -x of center
    ]
    v_mag = 0.5
    velocities = compute_radial_inlet_velocities(face_centers, opening_center, "ceiling", v_mag)

    assert len(velocities) == 3
    for v in velocities:
        assert math.isclose(math.sqrt(sum(c * c for c in v)), v_mag, rel_tol=1e-9)

    vx0, vy0, vz0 = velocities[0]
    assert vx0 > 0  # spreads away from center in +x
    assert math.isclose(vy0, 0.0, abs_tol=1e-9)
    vx1, vy1, vz1 = velocities[1]
    assert vy1 > 0  # spreads away from center in +y
    vx2, vy2, vz2 = velocities[2]
    assert vx2 < 0  # spreads away from center in -x

    # ceiling's inward normal is (0,0,-1) - every face should get the same
    # small downward (negative z) tilt into the room.
    for v in velocities:
        assert v[2] < 0
        assert math.isclose(v[2], -v_mag * math.sin(math.radians(15)), rel_tol=1e-6)


def test_radial_velocities_generalize_to_a_side_wall():
    # xMin's in-plane axes are (y,z), inward normal (1,0,0) - a face
    # offset only in y from the opening center should have zero
    # z-component and a positive x-component (tilted into the room).
    opening_center = (0.0, 1.5, 1.2)
    face_centers = [(0.0, 1.8, 1.2)]
    v_mag = 0.3
    v = compute_radial_inlet_velocities(face_centers, opening_center, "xMin", v_mag)[0]
    assert math.isclose(math.sqrt(sum(c * c for c in v)), v_mag, rel_tol=1e-9)
    assert v[1] > 0  # spreads toward +y, away from center
    assert math.isclose(v[2], 0.0, abs_tol=1e-9)  # no z offset given, no z spread
    assert v[0] > 0  # tilted into the room along xMin's inward normal (+x)


def test_radial_velocity_falls_back_to_normal_when_face_is_at_center():
    # A degenerate case: face center coincides exactly with the opening
    # center - no radial direction is defined, must not divide by zero.
    v = compute_radial_inlet_velocities([(1.0, 1.0, 1.0)], (1.0, 1.0, 1.0), "floor", 0.4)[0]
    assert v == pytest.approx(tuple(0.4 * d for d in WALL_INFLOW_DIRECTION["floor"]))


def test_radial_velocities_return_plain_floats_not_numpy():
    # Must be JSON-serializable once stored in results.json/summary dicts.
    v = compute_radial_inlet_velocities([(2.3, 1.5, 2.7)], (2.0, 1.5, 2.7), "ceiling", 0.5)[0]
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
    result = resolve_inlet_velocity("case123", "inlet", "ceiling", (0, 1.5, 2.7), 0.5, diffuser_type="ceiling")

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
