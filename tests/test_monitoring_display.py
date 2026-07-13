from guvcfd.app import _gather_monitoring_points, _monitoring_summary_rows
from guvcfd.report import _monitoring_rows


def _base_settings(**overrides):
    settings = {"monitoring-enable": True}
    for i in (1, 2, 3):
        settings[f"monitor{i}-enable"] = False
        settings[f"monitor{i}-name"] = f"Point {i}"
        settings[f"monitor{i}-x-input"] = float(i)
        settings[f"monitor{i}-y-input"] = 1.5
        settings[f"monitor{i}-z-input"] = 1.5
        settings[f"monitor{i}-cells"] = 4
    settings.update(overrides)
    return settings


def test_gather_returns_empty_when_master_toggle_off():
    settings = _base_settings(**{"monitor1-enable": True, "monitoring-enable": False})
    assert _gather_monitoring_points(settings) == []


def test_gather_returns_empty_when_no_point_enabled():
    settings = _base_settings()
    assert _gather_monitoring_points(settings) == []


def test_gather_returns_only_enabled_points():
    settings = _base_settings(**{"monitor1-enable": True, "monitor3-enable": True})
    points = _gather_monitoring_points(settings)
    names = [p["name"] for p in points]
    assert names == ["Point 1", "Point 3"]
    assert points[0] == {"name": "Point 1", "x": 1.0, "y": 1.5, "z": 1.5, "cells_per_side": 4}


def test_gather_falls_back_to_default_name_if_blank():
    settings = _base_settings(**{"monitor2-enable": True, "monitor2-name": ""})
    points = _gather_monitoring_points(settings)
    assert points[0]["name"] == "Point 2"


def test_decay_style_summary_rows():
    monitoring = {
        "Patient": {"t_seconds": [0, 10], "volAverage_T": [1.0, 0.5], "eACH_uv_effective": 12.3},
    }
    rows = _monitoring_summary_rows(monitoring)
    assert rows[0] == ("Monitoring locations", "")
    label, value = rows[1]
    assert "Patient" in label
    assert "0.5" in value
    assert "12.3" in value


def test_steady_state_style_summary_rows():
    monitoring = {
        "Exhaust": {
            "phase1": {"t_seconds": [0, 100], "volAverage_T": [0.0, 0.2]},
            "phase2": {"t_seconds": [0, 100], "volAverage_T": [0.2, 0.05]},
        },
    }
    rows = _monitoring_summary_rows(monitoring)
    label, value = rows[1]
    assert "Exhaust" in label
    assert "T_ss1=0.2" in value
    assert "T_ss2=0.05" in value
    assert "75.0%" in value  # (1 - 0.05/0.2) * 100


def test_empty_monitoring_gives_no_rows():
    assert _monitoring_summary_rows(None) == []
    assert _monitoring_summary_rows({}) == []


def test_report_monitoring_rows_decay_style():
    monitoring = {"Patient": {"t_seconds": [0, 10], "volAverage_T": [1.0, 0.5], "eACH_uv_effective": 12.3}}
    rows = _monitoring_rows(monitoring)
    assert rows == [("Patient", "final volAverage(T)=0.5, eACH_uv=12.3/hr")]


def test_report_monitoring_rows_steady_state_style():
    monitoring = {
        "Exhaust": {
            "phase1": {"t_seconds": [0, 100], "volAverage_T": [0.0, 0.2]},
            "phase2": {"t_seconds": [0, 100], "volAverage_T": [0.2, 0.05]},
        },
    }
    rows = _monitoring_rows(monitoring)
    assert rows == [("Exhaust", "T_ss1=0.2, T_ss2=0.05, reduction=75.0%")]
