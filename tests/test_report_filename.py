import json

from guvcfd.app import _default_report_name, _loaded


def test_uses_run_settings_guv_path_over_currently_loaded_project(tmp_path, monkeypatch):
    # This is the actual bug: the Analysis tab can be showing a run from a
    # completely different (or no) project than whatever's currently open
    # on the Setup tab - the report filename must reflect the run being
    # reported on, not incidental global GUI state.
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "run_settings.json").write_text(
        json.dumps({"guv_path": r"C:\projects\patient ward 4B1 v4.guv"}))

    monkeypatch.setitem(_loaded, "settings_path", r"C:\projects\some_other_unrelated_project.guvcfd")

    assert _default_report_name(str(case_dir)) == "patient ward 4B1 v4_report.docx"


def test_falls_back_to_loaded_project_when_run_settings_missing(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    monkeypatch.setitem(_loaded, "settings_path", r"C:\projects\myproject.guvcfd")

    assert _default_report_name(str(case_dir)) == "myproject_report.docx"


def test_falls_back_to_case_dir_name_when_nothing_else_available(tmp_path, monkeypatch):
    case_dir = tmp_path / "some_case_folder"
    case_dir.mkdir()
    monkeypatch.setitem(_loaded, "settings_path", None)

    assert _default_report_name(str(case_dir)) == "some_case_folder_report.docx"


def test_falls_back_when_run_settings_has_no_guv_path(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "run_settings.json").write_text(json.dumps({"ach": 3.0}))
    monkeypatch.setitem(_loaded, "settings_path", r"C:\projects\myproject.guvcfd")

    assert _default_report_name(str(case_dir)) == "myproject_report.docx"
