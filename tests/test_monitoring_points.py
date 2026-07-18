from guvcfd.monitoring_points import (
    zone_name, monitoring_topo_set_dict, monitoring_average_dict, mixing_uniformity_note,
)


def test_zone_name_sanitizes_spaces_and_punctuation():
    assert zone_name("Patient Bed 1") == "Patient_Bed_1"
    assert zone_name("HCW's desk") == "HCW_s_desk"


def test_zone_name_prefixes_leading_digit():
    assert zone_name("1st point") == "pt_1st_point"


def test_zone_name_falls_back_when_empty():
    assert zone_name("...") == "monitor"


def test_topo_set_dict_has_one_box_per_point():
    points = [
        {"name": "Patient", "x": 1.0, "y": 1.5, "z": 1.2, "cells_per_side": 4},
        {"name": "Exhaust", "x": 3.9, "y": 1.5, "z": 2.4, "cells_per_side": 2},
    ]
    text = monitoring_topo_set_dict(points, cell_size=0.1)
    assert text.count("boxToCell") == 2
    assert text.count("setToCellZone") == 2
    assert "PatientCells" in text
    assert "ExhaustCells" in text
    # Patient: cells_per_side=4, cell_size=0.1 -> box side 0.4, centered at
    # x=1.0 -> box spans x in [0.8, 1.2].
    assert "0.8 1.3 1" in text  # lo corner (y=1.5-0.2=1.3, z=1.2-0.2=1.0)


def test_topo_set_dict_snaps_off_grid_center_to_nearest_grid_line():
    # An arbitrary, not-grid-aligned position (1.73, 1.47, 1.21) on a
    # cell_size=0.1 mesh - each box edge should land on an exact multiple
    # of 0.1, not the raw unsnapped value, avoiding a boxToCell floating-
    # point boundary tie right where the user happened to click.
    points = [{"name": "Patient", "x": 1.73, "y": 1.47, "z": 1.21, "cells_per_side": 4}]
    text = monitoring_topo_set_dict(points, cell_size=0.1)
    import re
    m = re.search(r"box\s+\(([^)]*)\)\s+\(([^)]*)\)", text)
    lo = [float(v) for v in m.group(1).split()]
    hi = [float(v) for v in m.group(2).split()]
    for v in lo + hi:
        assert abs(round(v / 0.1) * 0.1 - v) < 1e-9


def test_topo_set_dict_snap_is_noop_for_already_aligned_center():
    points = [{"name": "Exhaust", "x": 3.9, "y": 1.5, "z": 2.4, "cells_per_side": 2}]
    text = monitoring_topo_set_dict(points, cell_size=0.1)
    assert "3.8 1.4 2.3" in text  # lo corner: 3.9-0.1, 1.5-0.1, 2.4-0.1 (size=0.2)


def test_average_dict_has_one_volfieldvalue_per_point_and_shared_read():
    points = [
        {"name": "Patient", "x": 1.0, "y": 1.5, "z": 1.2, "cells_per_side": 4},
        {"name": "Exhaust", "x": 3.9, "y": 1.5, "z": 2.4, "cells_per_side": 2},
    ]
    text = monitoring_average_dict(points, field="T")
    assert text.count("readT") == 1
    assert text.count("volFieldValue") == 2
    assert "monitor_Patient" in text
    assert "monitor_Exhaust" in text
    assert text.count("regionType      cellZone;") == 2


def test_average_dict_uses_given_field_name():
    points = [{"name": "P", "x": 0, "y": 0, "z": 0, "cells_per_side": 4}]
    text = monitoring_average_dict(points, field="U")
    assert "readU" in text
    assert "fields          (U);" in text


def test_mixing_uniformity_note_none_without_monitoring():
    assert mixing_uniformity_note({}) is None
    assert mixing_uniformity_note({"monitoring": {}}) is None


def test_mixing_uniformity_note_none_when_points_track_room_average():
    result = {
        "decay_curve": {"volAverage_T": [1.0, 0.5, 0.25]},
        "monitoring": {"Patient": {"volAverage_T": [1.0, 0.52, 0.26]}},
    }
    assert mixing_uniformity_note(result) is None


def test_mixing_uniformity_note_flags_decay_scenario_deviation():
    result = {
        "decay_curve": {"volAverage_T": [1.0, 0.5, 0.25]},
        "monitoring": {"Patient": {"volAverage_T": [1.0, 0.4, 0.10]}},
    }
    note = mixing_uniformity_note(result)
    assert note is not None
    assert "NOT well mixed" in note
    assert "Patient" in note
    assert "60%" in note  # (0.25 - 0.10) / 0.25 = 60% below


def test_mixing_uniformity_note_flags_steady_state_scenario_deviation():
    # Real numbers from a completed run: room-average vs monitoring points
    # differ by 20-70% - exactly the case this note exists to catch.
    result = {
        "phase1": {"T_ss": 0.254927},
        "phase2": {"T_ss": 0.03959168},
        "monitoring": {
            "Patient": {
                "phase1": {"volAverage_T": [0.0, 0.1957093]},
                "phase2": {"volAverage_T": [0.1957093, 0.01215402]},
            },
            "exhaust": {
                "phase1": {"volAverage_T": [0.0, 0.3078411]},
                "phase2": {"volAverage_T": [0.3078411, 0.05844288]},
            },
        },
    }
    note = mixing_uniformity_note(result)
    assert note is not None
    assert "NOT well mixed" in note
    assert "Patient" in note and "exhaust" in note
