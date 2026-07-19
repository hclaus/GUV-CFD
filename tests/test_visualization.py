from guv_calcs import Project

from guvcfd.visualization import plot_case, _box_mesh

GUV_PATH = r"c:\Users\hukcl\Documents\Python\Illuminator2\illuminate-v4\4x3x2.7.guv"


def _load_room():
    project = Project.load(GUV_PATH)
    return next(iter(project.rooms.values()))


def test_box_mesh_has_eight_vertices_sized_around_center():
    x, y, z, i, j, k = _box_mesh((1.0, 2.0, 1.5), size=0.4)
    assert len(x) == len(y) == len(z) == 8
    assert min(x) == 1.0 - 0.2 and max(x) == 1.0 + 0.2
    assert min(y) == 2.0 - 0.2 and max(y) == 2.0 + 0.2
    assert min(z) == 1.5 - 0.2 and max(z) == 1.5 + 0.2
    # 6 faces * 2 triangles = 12
    assert len(i) == len(j) == len(k) == 12


def test_plot_case_without_monitoring_points_has_no_monitor_traces():
    room = _load_room()
    fig = plot_case(room)
    tags = [str(t.customdata[0]) for t in fig.data if t.customdata]
    assert not any("monitor" in tag for tag in tags)


def test_plot_case_camera_faces_the_origin_corner():
    # The default (inherited from guv_calcs' RoomPlotter) puts the camera
    # in the +x/+y octant, so the (xmax, ymax) corner faces the viewer and
    # the (0, 0) origin corner is hidden around the back - plot_case flips
    # this so (0, 0) is the near, front-facing corner instead.
    room = _load_room()
    fig = plot_case(room)
    eye = fig.layout.scene.camera.eye
    assert eye.x < 0 and eye.y < 0


def test_plot_case_draws_one_box_and_label_per_monitoring_point():
    room = _load_room()
    points = [
        {"name": "Patient", "x": 1.0, "y": 1.5, "z": 1.2, "cells_per_side": 4},
        {"name": "Exhaust", "x": 3.0, "y": 1.5, "z": 0.4, "cells_per_side": 2},
    ]
    fig = plot_case(room, monitoring_points=points, cell_size=0.1)
    tags = [str(t.customdata[0]) for t in fig.data if t.customdata]
    assert "Patient_monitor_volume" in tags
    assert "Patient_monitor_label" in tags
    assert "Exhaust_monitor_volume" in tags
    assert "Exhaust_monitor_label" in tags


def test_plot_case_monitoring_box_size_matches_cells_per_side():
    room = _load_room()
    points = [{"name": "Patient", "x": 1.0, "y": 1.5, "z": 1.2, "cells_per_side": 4}]
    fig = plot_case(room, monitoring_points=points, cell_size=0.1)
    box_trace = next(t for t in fig.data if t.customdata and t.customdata[0] == "Patient_monitor_volume")
    # cells_per_side=4, cell_size=0.1 -> box side 0.4, centered at x=1.0.
    assert min(box_trace.x) == 1.0 - 0.2 and max(box_trace.x) == 1.0 + 0.2


def test_plot_case_without_second_openings_has_no_inlet2_outlet2_traces():
    room = _load_room()
    fig = plot_case(room)
    tags = [str(t.customdata[0]) for t in fig.data if t.customdata]
    assert not any(tag.startswith("inlet2") or tag.startswith("outlet2") for tag in tags)


def test_plot_case_draws_second_inlet_and_outlet_when_given():
    room = _load_room()
    fig = plot_case(
        room,
        inlet2_wall="ceiling", inlet2_center=(0.5, 0.5), inlet2_size=(0.2, 0.2),
        outlet2_wall="floor", outlet2_center=(0.5, 0.5), outlet2_size=(0.2, 0.2),
    )
    tags = [str(t.customdata[0]) for t in fig.data if t.customdata]
    assert "inlet2_outline" in tags
    assert "outlet2_outline" in tags


def test_plot_case_opening_on_floor_stays_in_the_xy_plane():
    room = _load_room()
    fig = plot_case(room, inlet_wall="floor", inlet_center=(0.5, 0.5), inlet_size=(0.3, 0.3))
    outline = next(t for t in fig.data if t.customdata and t.customdata[0] == "inlet_outline")
    assert all(abs(z) < 1e-6 for z in outline.z)  # floor is z=0 - must not vary in z
    assert max(outline.x) - min(outline.x) > 0  # but must vary in x and y
    assert max(outline.y) - min(outline.y) > 0


def test_plot_case_inlet_arrow_points_into_the_room_from_ceiling():
    room = _load_room()
    fig = plot_case(room, inlet_wall="ceiling", inlet_center=(0.5, 0.5), inlet_size=(0.3, 0.3))
    arrow = next(t for t in fig.data if t.customdata and t.customdata[0] == "inlet_arrow")
    assert arrow.z[1] < arrow.z[0]  # ceiling's inward normal points down
