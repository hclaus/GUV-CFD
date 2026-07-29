from guvcfd.splice import disable_simple_residual_control

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
        U
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
        "(k|omega)"     1e-4;
    }
}
"""


def _write_fv_solution(tmp_path, content=_FV_SOLUTION):
    case_dir = tmp_path / "case"
    (case_dir / "system").mkdir(parents=True)
    (case_dir / "system" / "fvSolution").write_text(content)
    return str(case_dir)


def test_empties_only_simples_residual_control(tmp_path):
    case_dir = _write_fv_solution(tmp_path)
    disable_simple_residual_control(case_dir)
    content = (tmp_path / "case" / "system" / "fvSolution").read_text()
    assert content.count("{") == content.count("}")

    simple_block = content[content.index("SIMPLE"):]
    assert "residualControl\n    {}" in simple_block


def test_does_not_corrupt_pimples_nested_residual_control(tmp_path):
    # Regression: a naive '[^}]*' scan for "residualControl { ... }" stops at
    # the FIRST '}' it finds - for PIMPLE's NESTED block (p{...}/U{...} sub-
    # dicts, unlike SIMPLE's flat p 1e-4;/U 1e-4;), that's the inner p{}
    # sub-dict's closing brace, truncating the rest of PIMPLE's block and
    # leaving a dangling '}' - broke every real Phase 1 run with a
    # "FOAM FATAL IO ERROR: Unexpected '}'" the first time this coexisted
    # with PIMPLE.residualControl in the real template.
    case_dir = _write_fv_solution(tmp_path)
    disable_simple_residual_control(case_dir)
    content = (tmp_path / "case" / "system" / "fvSolution").read_text()

    pimple_block = content[content.index("PIMPLE"):content.index("SIMPLE")]
    assert "p\n        {\n            tolerance   1e-4;\n            relTol      0;\n        }" in pimple_block
    assert "U\n        {\n            tolerance   1e-4;\n            relTol      0;\n        }" in pimple_block


def test_idempotent(tmp_path):
    case_dir = _write_fv_solution(tmp_path)
    disable_simple_residual_control(case_dir)
    first = (tmp_path / "case" / "system" / "fvSolution").read_text()
    disable_simple_residual_control(case_dir)
    second = (tmp_path / "case" / "system" / "fvSolution").read_text()
    assert first == second


def test_noop_when_no_simple_block(tmp_path):
    content = "PIMPLE\n{\n    nOuterCorrectors 1;\n}\n"
    case_dir = _write_fv_solution(tmp_path, content)
    disable_simple_residual_control(case_dir)
    assert (tmp_path / "case" / "system" / "fvSolution").read_text() == content
