from guvcfd.mesh_gen import _opening_box, topo_set_dict, create_patch_dict, write_mesh_dicts

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
