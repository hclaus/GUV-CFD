from guvcfd.splice import set_relaxation_factors, compute_adaptive_scalar_relaxation

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


def test_compute_adaptive_scalar_relaxation_matches_calibration_direction():
    # Real calibration data (2026-08-24/25, patient_ward_4B1_v7cell008):
    # last-confirmed-stable relaxation at each kUV.max. The formula must
    # predict AT OR BELOW each stable value (a safety margin, never an
    # overshoot into the known-crashing region).
    assert compute_adaptive_scalar_relaxation(8.08) <= 0.25
    assert compute_adaptive_scalar_relaxation(14.15) <= 0.15
    assert compute_adaptive_scalar_relaxation(20.21) <= 0.10


def test_compute_adaptive_scalar_relaxation_is_monotonically_decreasing():
    values = [compute_adaptive_scalar_relaxation(k) for k in (1, 2, 4, 8, 16, 32, 64)]
    assert values == sorted(values, reverse=True)


def test_compute_adaptive_scalar_relaxation_clips_to_bounds():
    assert compute_adaptive_scalar_relaxation(0.001) == 0.7  # near-zero kUV -> template default ceiling
    assert compute_adaptive_scalar_relaxation(0) == 0.7
    assert compute_adaptive_scalar_relaxation(1000) == 0.05  # extreme kUV -> floor, not near-zero


def test_compute_adaptive_scalar_relaxation_is_rounded_not_raw_float():
    # 1.8/4.04 is 0.44554455445544555 unrounded - false precision for a
    # calibration only pinned to ~10-15% margin, and an ugly value to land
    # in fvSolution or a saved project file. Must come back at 3dp.
    value = compute_adaptive_scalar_relaxation(4.04)
    assert value == 0.446
    assert round(value, 3) == value
