from pathlib import Path

import pytest

from guvcfd.tclamp_decay import (
    source_zone_max_T, tclamp_decay_function_object, splice_tclamp_decay_if_needed,
)

_CONTROL_DICT = """FoamFile
{
    version     2.0;
    format      ascii;
}

functions
{
    scalarTransport1
    {
        enabled         true;
        type            scalarTransport;
    }

    volAverageLive1
    {
        type            volFieldValue;
        fields          (T);
    }
}
"""


def _write_scalar_field_file(path, values):
    body = "\n".join(str(v) for v in values)
    path.write_text(
        "FoamFile\n{\n    class volScalarField;\n    object T;\n}\n\n"
        f"internalField   nonuniform List<scalar>\n{len(values)}\n(\n{body}\n)\n;\n"
    )


def _make_case(tmp_path, centers, t_values, snapshot_name="phase1_T.snapshot"):
    case_dir = tmp_path / "case"
    (case_dir / "0").mkdir(parents=True)
    (case_dir / "system").mkdir()
    _write_scalar_field_file(case_dir / "0" / "Cx", [c[0] for c in centers])
    _write_scalar_field_file(case_dir / "0" / "Cy", [c[1] for c in centers])
    _write_scalar_field_file(case_dir / "0" / "Cz", [c[2] for c in centers])
    _write_scalar_field_file(case_dir / snapshot_name, t_values)
    (case_dir / "system" / "controlDict").write_text(_CONTROL_DICT)
    return str(case_dir)


# A 3x3x3 grid of cell centers at 0.1m spacing, centered on (0.5, 0.5, 0.5) -
# only the single center cell (0.5, 0.5, 0.5) falls inside a small
# source box of size 0.05m there.
_GRID = [(round(0.4 + 0.1 * i, 2), round(0.4 + 0.1 * j, 2), round(0.4 + 0.1 * k, 2))
         for i in range(3) for j in range(3) for k in range(3)]


def test_source_zone_max_t_only_considers_cells_inside_the_source_box(tmp_path):
    # Give the center cell (index 13, (0.5,0.5,0.5)) the highest T, and a
    # cell clearly outside the box an even higher T - must be ignored.
    t_values = [1.0] * len(_GRID)
    center_idx = _GRID.index((0.5, 0.5, 0.5))
    t_values[center_idx] = 42.0
    outside_idx = _GRID.index((0.4, 0.4, 0.4))
    t_values[outside_idx] = 999.0
    case_dir = _make_case(tmp_path, _GRID, t_values)

    result = source_zone_max_T(case_dir, source_center=(0.5, 0.5, 0.5), source_size=0.05)
    assert result == 42.0


def test_source_zone_max_t_picks_the_max_among_multiple_cells_in_the_box(tmp_path):
    t_values = [1.0] * len(_GRID)
    # A 0.25m box around (0.5,0.5,0.5) (half-extent 0.125) covers the
    # center cell and its 6 face neighbors at 0.1m spacing.
    neighbor_idx = _GRID.index((0.6, 0.5, 0.5))
    t_values[neighbor_idx] = 7.5
    center_idx = _GRID.index((0.5, 0.5, 0.5))
    t_values[center_idx] = 3.0
    case_dir = _make_case(tmp_path, _GRID, t_values)

    result = source_zone_max_T(case_dir, source_center=(0.5, 0.5, 0.5), source_size=0.25)
    assert result == 7.5


def test_source_zone_max_t_raises_when_box_contains_no_cells(tmp_path):
    case_dir = _make_case(tmp_path, _GRID, [1.0] * len(_GRID))
    with pytest.raises(RuntimeError, match="no cells found"):
        source_zone_max_T(case_dir, source_center=(5.0, 5.0, 5.0), source_size=0.05)


def test_source_zone_max_t_raises_on_field_cell_count_mismatch(tmp_path):
    case_dir = _make_case(tmp_path, _GRID, [1.0] * len(_GRID))
    # Corrupt the snapshot to have fewer values than Cx/Cy/Cz.
    _write_scalar_field_file(Path(case_dir) / "phase1_T.snapshot", [1.0] * (len(_GRID) - 1))
    with pytest.raises(RuntimeError, match="mesh/field mismatch"):
        source_zone_max_T(case_dir, source_center=(0.5, 0.5, 0.5), source_size=0.15)


def test_tclamp_decay_function_object_has_expected_fields():
    block = tclamp_decay_function_object(29.913, field="T")
    assert "type            TClampDecay;" in block
    assert 'libs            ("libTClampDecayFunctionObject.so");' in block
    assert "field           T;" in block
    assert "Tmax            29.913;" in block


def test_tclamp_decay_function_object_formats_extreme_values_compactly():
    block = tclamp_decay_function_object(1.23456789e8)
    assert "Tmax            1.23457e+08;" in block


def test_splice_tclamp_decay_if_needed_inserts_and_is_idempotent(tmp_path):
    case_dir = _make_case(tmp_path, _GRID, [1.0] * len(_GRID))
    splice_tclamp_decay_if_needed(case_dir, 29.9)
    content = (tmp_path / "case" / "system" / "controlDict").read_text()
    assert "TClampDecay1" in content
    assert content.count("TClampDecay1") == 1
    assert "scalarTransport1" in content  # existing entry untouched

    # Second call must be a no-op, not a duplicate/brace-breaking splice.
    splice_tclamp_decay_if_needed(case_dir, 29.9)
    content2 = (tmp_path / "case" / "system" / "controlDict").read_text()
    assert content2 == content


def test_splice_tclamp_decay_runs_before_volaverage_tracking(tmp_path):
    # OpenFOAM executes function objects in dict order (functionObjectList
    # iterates the dictionary's own read order) - TClampDecay must sit
    # BEFORE volAverageLive1 so a room-average never includes a
    # momentarily out-of-[0,Tmax] cell that hasn't been corrected yet for
    # that same timestep (see ANALYSIS_LOG.md).
    case_dir = _make_case(tmp_path, _GRID, [1.0] * len(_GRID))
    splice_tclamp_decay_if_needed(case_dir, 29.9)
    content = (tmp_path / "case" / "system" / "controlDict").read_text()
    assert content.index("scalarTransport1") < content.index("TClampDecay1") < content.index("volAverageLive1")
