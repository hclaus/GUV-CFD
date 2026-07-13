from guvcfd.monitoring_points import zone_name, monitoring_topo_set_dict, monitoring_average_dict


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
