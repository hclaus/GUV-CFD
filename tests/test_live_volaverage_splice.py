from guvcfd.monitoring import live_vol_average_functions
from guvcfd.splice import splice_into_functions_block, set_function_write_interval

# Mirrors the real template (guvcfd/templates/case_template/system/controlDict):
# a functions{} block already containing scalarTransport1, which the live
# splice must sit alongside rather than replace.
_CONTROL_DICT = """FoamFile
{
    version     2.0;
    format      ascii;
}

endTime          8000;
writeInterval    200;

functions
{
    scalarTransport1
    {
        enabled         true;
        type            scalarTransport;
        executeControl  timeStep;
        executeInterval 1;
        writeControl    adjustableRunTime;
        writeInterval   200;
    }
}
"""


def _write_control_dict(tmp_path, content=_CONTROL_DICT):
    case_dir = tmp_path / "case"
    (case_dir / "system").mkdir(parents=True)
    (case_dir / "system" / "controlDict").write_text(content)
    return str(case_dir)


def test_splice_inserts_live_block_alongside_existing_scalar_transport(tmp_path):
    case_dir = _write_control_dict(tmp_path)
    block = live_vol_average_functions(field="T", patches=("outlet",), monitoring_zones=("Patient",))
    _, n_open, n_close = splice_into_functions_block(case_dir, block)
    assert n_open == n_close

    content = (tmp_path / "case" / "system" / "controlDict").read_text()
    assert "scalarTransport1" in content  # existing entry untouched
    assert "volAverageLive1" in content
    assert "outletAverageLive" in content
    assert "monitor_PatientLive" in content


def test_spliced_live_block_uses_region_type_all_for_room_and_cellzone_for_points(tmp_path):
    case_dir = _write_control_dict(tmp_path)
    block = live_vol_average_functions(field="T", patches=(), monitoring_zones=("Patient",))
    splice_into_functions_block(case_dir, block)
    content = (tmp_path / "case" / "system" / "controlDict").read_text()

    room_block = content.split("volAverageLive1")[1].split("monitor_PatientLive")[0]
    assert "regionType      all;" in room_block

    point_block = content.split("monitor_PatientLive")[1]
    assert "regionType      cellZone;" in point_block
    assert "name            Patient;" in point_block


def test_live_block_defaults_to_every_iteration(tmp_path):
    case_dir = _write_control_dict(tmp_path)
    block = live_vol_average_functions(field="T", patches=(), monitoring_zones=())
    splice_into_functions_block(case_dir, block)
    content = (tmp_path / "case" / "system" / "controlDict").read_text()
    live_block = content.split("volAverageLive1")[1].split("}")[0]
    assert "executeInterval 1;" in live_block
    assert "writeInterval   1;" in live_block


def test_set_function_write_interval_only_touches_named_block(tmp_path):
    case_dir = _write_control_dict(tmp_path)
    block = live_vol_average_functions(field="T", patches=(), monitoring_zones=())
    splice_into_functions_block(case_dir, block)

    # Simulate a later phase's set_control_dict_time() sweeping every
    # writeInterval in the file to a sparser value - the exact scenario
    # set_function_write_interval exists to correct.
    content = (tmp_path / "case" / "system" / "controlDict").read_text()
    content = content.replace("writeInterval   1;", "writeInterval   100;")
    (tmp_path / "case" / "system" / "controlDict").write_text(content)

    set_function_write_interval(case_dir, "volAverageLive1", 1)
    fixed = (tmp_path / "case" / "system" / "controlDict").read_text()

    live_block = fixed.split("volAverageLive1")[1].split("}")[0]
    assert "writeInterval   1;" in live_block
    # scalarTransport1's own writeInterval must be left alone - only the
    # named live block gets pinned back.
    scalar_block = fixed.split("scalarTransport1")[1].split("}")[0]
    assert "writeInterval   200;" in scalar_block
