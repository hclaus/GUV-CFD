from guvcfd.splice import set_relaxation_factors

_FV_SOLUTION = """FoamFile
{
    version     2.0;
    format      ascii;
}

solvers
{
    "(U|k|omega|T)"
    {
        solver          smoothSolver;
    }
}

relaxationFactors
{
    fields
    {
        p               0.3;
    }
    equations
    {
        U               0.7;
        "(k|omega)"     0.7;
        T               0.7;
    }
}
"""


def _write_fv_solution(tmp_path, content=_FV_SOLUTION):
    case_dir = tmp_path / "case"
    (case_dir / "system").mkdir(parents=True)
    (case_dir / "system" / "fvSolution").write_text(content)
    return str(case_dir)


def test_sets_both_momentum_and_scalar_factors(tmp_path):
    case_dir = _write_fv_solution(tmp_path)
    set_relaxation_factors(case_dir, momentum_factor=0.5, scalar_factor=0.4)
    content = (tmp_path / "case" / "system" / "fvSolution").read_text()
    assert "U               0.5;" in content
    assert '"(k|omega)"     0.5;' in content
    assert "T               0.4;" in content
    assert "p               0.3;" in content  # untouched


def test_none_leaves_that_factor_untouched(tmp_path):
    case_dir = _write_fv_solution(tmp_path)
    set_relaxation_factors(case_dir, momentum_factor=0.6, scalar_factor=None)
    content = (tmp_path / "case" / "system" / "fvSolution").read_text()
    assert "U               0.6;" in content
    assert "T               0.7;" in content  # left at template default


def test_both_none_is_a_noop(tmp_path):
    case_dir = _write_fv_solution(tmp_path)
    set_relaxation_factors(case_dir)
    content = (tmp_path / "case" / "system" / "fvSolution").read_text()
    assert content == _FV_SOLUTION


def test_does_not_touch_the_solvers_block(tmp_path):
    # "(U|k|omega|T)" in the solvers{} block must not get corrupted by the
    # regex targeting relaxationFactors' bare "U"/"T" entries.
    case_dir = _write_fv_solution(tmp_path)
    set_relaxation_factors(case_dir, momentum_factor=0.5, scalar_factor=0.4)
    content = (tmp_path / "case" / "system" / "fvSolution").read_text()
    assert '"(U|k|omega|T)"' in content
