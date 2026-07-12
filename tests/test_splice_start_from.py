from guvcfd.splice import set_control_dict_start_from

_CONTROL_DICT = """FoamFile
{
    version     2.0;
    format      ascii;
}

startFrom        startTime;
startTime        0;
stopAt           endTime;
endTime          60;
"""


def _write_control_dict(tmp_path, content=_CONTROL_DICT):
    case_dir = tmp_path / "case"
    (case_dir / "system").mkdir(parents=True)
    (case_dir / "system" / "controlDict").write_text(content)
    return str(case_dir)


def test_switches_to_latest_time(tmp_path):
    case_dir = _write_control_dict(tmp_path)
    set_control_dict_start_from(case_dir, "latestTime")
    content = (tmp_path / "case" / "system" / "controlDict").read_text()
    assert "startFrom        latestTime;" in content
    assert "startTime        0;" in content  # untouched


def test_switches_back_to_start_time(tmp_path):
    case_dir = _write_control_dict(tmp_path, _CONTROL_DICT.replace("startTime;", "latestTime;", 1))
    set_control_dict_start_from(case_dir, "startTime")
    content = (tmp_path / "case" / "system" / "controlDict").read_text()
    assert "startFrom        startTime;" in content


def test_only_first_occurrence_replaced(tmp_path):
    # endTime's own value must never be touched by this function.
    case_dir = _write_control_dict(tmp_path)
    set_control_dict_start_from(case_dir, "latestTime")
    content = (tmp_path / "case" / "system" / "controlDict").read_text()
    assert "endTime          60;" in content
