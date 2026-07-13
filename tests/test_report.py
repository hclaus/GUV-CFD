import json

import pytest

from guvcfd.report import generate_report_docx


def test_missing_run_settings_raises_clear_error(tmp_path):
    case_dir = str(tmp_path)
    (tmp_path / "results.json").write_text("{}")
    with pytest.raises(FileNotFoundError, match="run a full simulation"):
        generate_report_docx(case_dir, str(tmp_path / "out.docx"))


def test_missing_results_raises_clear_error(tmp_path):
    case_dir = str(tmp_path)
    (tmp_path / "run_settings.json").write_text("{}")
    with pytest.raises(FileNotFoundError, match="run a full simulation"):
        generate_report_docx(case_dir, str(tmp_path / "out.docx"))


def test_missing_guv_path_raises_clear_error(tmp_path):
    case_dir = str(tmp_path)
    (tmp_path / "run_settings.json").write_text(json.dumps({"ach": 3.0}))  # no guv_path
    (tmp_path / "results.json").write_text("{}")
    with pytest.raises(FileNotFoundError, match="predates report support"):
        generate_report_docx(case_dir, str(tmp_path / "out.docx"))


_REAL_SETTINGS = {
    "ach": 3.0, "z-value": 2.0,
    "inlet-wall": "xMin", "inlet-y-input": 1.5, "inlet-z-input": 2.295,
    "inlet-size-w": 0.3, "inlet-size-h": 0.3,
    "outlet-wall": "xMax", "outlet-y-input": 1.5, "outlet-z-input": 0.405,
    "outlet-size-w": 0.3, "outlet-size-h": 0.3,
    "fan-enable": False,
    "guv_path": r"c:\Users\hukcl\Documents\Python\Illuminator2\illuminate-v4\4x3x2.7.guv",
}

_STEADY_STATE_RESULTS = {
    "target_T_ss": 0.3,
    "phase1": {"T_ss": 0.2548, "converged": True, "iterations": 8000},
    "phase2": {"T_ss": 0.0644, "converged": False, "iterations": 3000},
    "reduction_pct": 74.7,
    "eACH_uv_steady_state": 17.73,
    "fluence_mean": 12.34,
}


def test_steady_state_report_does_not_crash_on_decay_only_fields(tmp_path):
    # Regression test: steady-state results.json has a totally different
    # schema (phase1/phase2/reduction_pct/eACH_uv_steady_state) than decay's
    # (ventilation_ach/eACH_uv_effective/decay_curve) - generate_report_docx
    # must dispatch on scenario type instead of assuming decay's fields.
    case_dir = str(tmp_path)
    (tmp_path / "run_settings.json").write_text(json.dumps(_REAL_SETTINGS))
    (tmp_path / "results.json").write_text(json.dumps(_STEADY_STATE_RESULTS))
    out_path = str(tmp_path / "out.docx")

    generate_report_docx(case_dir, out_path)

    from docx import Document
    doc = Document(out_path)
    all_text = "\n".join(p.text for p in doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            all_text += "\n" + "\t".join(c.text for c in row.cells)
    assert "74.7%" in all_text
    assert "17.73" in all_text
    assert "12.34" in all_text  # average fluence rate
    assert len(doc.inline_shapes) == 1  # room preview picture embedded


def test_decay_report_shows_fluence_mean(tmp_path):
    case_dir = str(tmp_path)
    (tmp_path / "run_settings.json").write_text(json.dumps(_REAL_SETTINGS))
    decay_results = {
        "ventilation_ach": 3.0, "eACH_uv_well_mixed": 10.27, "eACH_uv_effective": 8.97,
        "mixing_efficiency": 0.873, "total_ach_effective": 11.97,
        "decay_curve": {"t_seconds": [0, 10], "volAverage_T": [1.0, 0.9]},
        "fluence_mean": 5.678,
    }
    (tmp_path / "results.json").write_text(json.dumps(decay_results))
    out_path = str(tmp_path / "out.docx")

    generate_report_docx(case_dir, out_path)

    from docx import Document
    doc = Document(out_path)
    all_text = ""
    for table in doc.tables:
        for row in table.rows:
            all_text += "\n" + "\t".join(c.text for c in row.cells)
    assert "5.678" in all_text


def test_steady_state_report_shows_corrected_fields_when_present(tmp_path):
    case_dir = str(tmp_path)
    (tmp_path / "run_settings.json").write_text(json.dumps(_REAL_SETTINGS))
    results = dict(_STEADY_STATE_RESULTS)
    results["ventilation_ach_measured"] = 2.55
    results["eACH_uv_steady_state_corrected"] = 18.1
    (tmp_path / "results.json").write_text(json.dumps(results))
    out_path = str(tmp_path / "out.docx")

    generate_report_docx(case_dir, out_path)

    from docx import Document
    doc = Document(out_path)
    all_text = ""
    for table in doc.tables:
        for row in table.rows:
            all_text += "\n" + "\t".join(c.text for c in row.cells)
    assert "2.55" in all_text
    assert "18.1" in all_text
