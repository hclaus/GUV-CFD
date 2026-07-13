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
