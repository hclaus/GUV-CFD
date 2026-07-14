from guvcfd.initial_fields import (
    compute_inlet_velocity, compute_inlet_velocities, WALL_INFLOW_DIRECTION,
    boundary_field_block, field_file_content,
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
