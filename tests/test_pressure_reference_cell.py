from guvcfd.splice import set_pressure_reference_cell

_FV_SOLUTION = """FoamFile
{
    version     2.0;
    format      ascii;
}

PIMPLE
{
    nOuterCorrectors 3;
    nCorrectors      2;
    residualControl
    {
        p
        {
            tolerance   1e-4;
            relTol      0;
        }
    }
}

SIMPLE
{
    nNonOrthogonalCorrectors 0;
    consistent      no;
    residualControl
    {
        p               1e-4;
        U               1e-4;
    }
}
"""


def _write_fv_solution(tmp_path, content=_FV_SOLUTION):
    case_dir = tmp_path / "case"
    (case_dir / "system").mkdir(parents=True)
    (case_dir / "system" / "fvSolution").write_text(content)
    return str(case_dir)


def test_inserts_pref_into_both_pimple_and_simple(tmp_path):
    case_dir = _write_fv_solution(tmp_path)
    set_pressure_reference_cell(case_dir)
    content = (tmp_path / "case" / "system" / "fvSolution").read_text()
    assert content.count("{") == content.count("}")

    pimple_block = content[content.index("PIMPLE"):content.index("SIMPLE")]
    simple_block = content[content.index("SIMPLE"):]
    assert "pRefCell        0;" in pimple_block
    assert "pRefCell        0;" in simple_block


def test_custom_cell_index(tmp_path):
    case_dir = _write_fv_solution(tmp_path)
    set_pressure_reference_cell(case_dir, cell_index=42)
    content = (tmp_path / "case" / "system" / "fvSolution").read_text()
    assert "pRefCell        42;" in content


def test_idempotent(tmp_path):
    case_dir = _write_fv_solution(tmp_path)
    set_pressure_reference_cell(case_dir)
    first = (tmp_path / "case" / "system" / "fvSolution").read_text()
    set_pressure_reference_cell(case_dir)
    second = (tmp_path / "case" / "system" / "fvSolution").read_text()
    assert first == second


def test_does_not_touch_pimples_nested_residual_control(tmp_path):
    # Regression pattern shared with disable_simple_residual_control: a
    # naive scan could stop at the first '}' inside PIMPLE's nested
    # residualControl.p{...} sub-dict and corrupt the rest of the block.
    case_dir = _write_fv_solution(tmp_path)
    set_pressure_reference_cell(case_dir)
    content = (tmp_path / "case" / "system" / "fvSolution").read_text()
    pimple_block = content[content.index("PIMPLE"):content.index("SIMPLE")]
    assert "p\n        {\n            tolerance   1e-4;\n            relTol      0;\n        }" in pimple_block


def test_noop_when_pref_already_present(tmp_path):
    content = _FV_SOLUTION.replace(
        "SIMPLE\n{\n", "SIMPLE\n{\n    pRefCell        7;\n    pRefValue       0;\n")
    case_dir = _write_fv_solution(tmp_path, content)
    set_pressure_reference_cell(case_dir)
    result = (tmp_path / "case" / "system" / "fvSolution").read_text()
    simple_block = result[result.index("SIMPLE"):]
    assert simple_block.count("pRefCell") == 1
    assert "pRefCell        7;" in simple_block  # untouched, not overwritten
