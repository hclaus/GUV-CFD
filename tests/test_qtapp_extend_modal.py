"""Tests for the framework-independent logic in qtapp/extend_modal.py -
load_extend_status/scratch_dirs_for_status/_ach_label_for_dirs don't need a
QApplication (they touch no widgets), so these run as plain pytest tests -
matches this app's own established pattern of keeping logic separable from
widgets (run_state.py/sweep_state.py/helpers.py). The QDialog class itself
has no automated coverage here, matching every other Qt dialog in this
package (project_setup_tab/settings_dialog/analysis_tab) - none of them
have test coverage either; this was verified working via manual
construction (MainWindow + ExtendModifyDialog both build without error).
"""
from types import SimpleNamespace

from guvcfd.qtapp import extend_modal as em
from guvcfd.project_status import update_ach_base_status, update_combo_status


def test_ach_label_for_dirs_matches_scenario_runs_convention():
    assert em._ach_label_for_dirs(3.0) == "3"
    assert em._ach_label_for_dirs(0.0) == "sealed"
    assert em._ach_label_for_dirs(-1.0) == "sealed"


def test_load_extend_status_reports_missing_status_file(tmp_path):
    status, settings, room, guv_path, settings_path, error = em.load_extend_status(str(tmp_path))
    assert error is not None and "No recorded status" in error
    assert settings is None and room is None


def test_load_extend_status_loads_settings_and_room(tmp_path, monkeypatch):
    settings_path = tmp_path / "proj.guvcfd"
    settings_path.write_text('{"sim-type": "decay", "ach": 3}')
    update_combo_status(str(tmp_path), tmp_path.name, z=6.0, ach=3.0, guv_path="proj.guv",
                         settings_path=str(settings_path), sim_type="decay", status="done")

    fake_room = SimpleNamespace(x=4.0, y=5.0, z=2.7)
    fake_project = SimpleNamespace(rooms={"r": fake_room})
    monkeypatch.setattr(em.Project, "load", staticmethod(lambda path: fake_project))

    status, settings, room, guv_path, sp, error = em.load_extend_status(str(tmp_path))
    assert error is None
    assert settings == {"sim-type": "decay", "ach": 3}
    assert room is fake_room
    assert guv_path == "proj.guv"


def test_scratch_dirs_for_status_filters_to_existing_dirs(tmp_path):
    base_dir = tmp_path / "_base_ACH3"
    base_dir.mkdir()
    status = {"ach_bases": {
        "3": {"base_dir": str(base_dir), "control_dir": str(tmp_path / "_control_ACH3")},
    }}
    dirs = em.scratch_dirs_for_status(str(tmp_path), status)
    assert dirs == [f"{tmp_path}/_base_ACH3"]  # only the one that actually exists


def test_scratch_dirs_for_status_empty_when_no_ach_bases(tmp_path):
    assert em.scratch_dirs_for_status(str(tmp_path), {}) == []
