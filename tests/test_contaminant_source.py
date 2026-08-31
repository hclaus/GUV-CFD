from types import SimpleNamespace

import guvcfd.contaminant_source as contaminant_source
from guvcfd.contaminant_source import (
    breathing_inlet_momentum_source, breathing_inlet_velocity_constraint,
    check_mass_balance, source_topo_set_dict,
)


def test_source_topo_set_dict_no_snap_by_default():
    text = source_topo_set_dict((2.0, 1.5, 1.35), (0.3, 0.3, 0.3))
    assert "box     (1.85 1.35 1.2) (2.15 1.65 1.5)" in text


def test_source_topo_set_dict_snaps_edges_when_cell_size_given():
    # Same center/size as the no-snap case above - the raw box edges
    # (1.85/2.15 etc) sit almost exactly on a cell_size=0.1 grid line, a
    # boxToCell floating-point boundary tie. Snapped edges must instead be
    # exact multiples of cell_size.
    text = source_topo_set_dict((2.0, 1.5, 1.35), (0.3, 0.3, 0.3), cell_size=0.1, room_dims=(4.0, 3.0, 2.7))
    import re
    m = re.search(r"box\s+\(([^)]*)\)\s+\(([^)]*)\)", text)
    lo = [float(v) for v in m.group(1).split()]
    hi = [float(v) for v in m.group(2).split()]
    for v in lo + hi:
        assert abs(round(v / 0.1) * 0.1 - v) < 1e-9


def test_source_topo_set_dict_snap_never_collapses_to_zero_width():
    text = source_topo_set_dict((2.0, 1.5, 1.35), (0.02, 0.02, 0.02), cell_size=0.1, room_dims=(4.0, 3.0, 2.7))
    import re
    m = re.search(r"box\s+\(([^)]*)\)\s+\(([^)]*)\)", text)
    lo = [float(v) for v in m.group(1).split()]
    hi = [float(v) for v in m.group(2).split()]
    for l, h in zip(lo, hi):
        assert h - l >= 0.1 - 1e-9  # float roundoff, e.g. 1.4-1.3 == 0.09999999999999998


def test_source_topo_set_dict_matches_real_case_that_used_to_shrink():
    # Regression test for the real bug: this exact combination (patient
    # ward injection point (2, 1.5, 1.5), 0.3m cube, 0.1m mesh) used to
    # silently snap DOWN to a 0.3x0.2x0.2m box (0.012 m^3, 44% of the
    # requested 0.027 m^3) under round-to-nearest, because two of its
    # three axes landed exactly on a rounding tie - confirmed directly
    # against the real steady-state results.json this produced
    # (source_volume: 0.012 m^3). The outward-snap fix must not reproduce
    # that shrinkage.
    import re
    text = source_topo_set_dict((2.0, 1.5, 1.5), (0.3, 0.3, 0.3), cell_size=0.1, room_dims=(4.0, 3.0, 2.7))
    m = re.search(r"box\s+\(([^)]*)\)\s+\(([^)]*)\)", text)
    lo = [float(v) for v in m.group(1).split()]
    hi = [float(v) for v in m.group(2).split()]
    volume = 1.0
    for l, h in zip(lo, hi):
        assert h - l >= 0.3 - 1e-9
        volume *= h - l
    assert volume >= 0.027 - 1e-9


def test_source_box_snaps_to_the_actual_mesh_grid_not_the_nominal_cell_size():
    # Regression test for a real, confirmed bug (2026-08-19, same root
    # cause as mesh_gen._opening_box's identical bug): _source_box used to
    # snap against the raw nominal cell_size, but block_mesh_dict builds
    # n = round(length/cell_size) cells spanning the room's EXACT
    # dimension - whenever length/cell_size isn't a whole number, the
    # ACTUAL per-axis cell size differs from nominal, and snapping
    # against the wrong grid lands carved edges off every real mesh grid
    # line (confirmed: an 0.8m source cube on this exact room at 0.09m
    # nominal came out as 1000 real cells instead of the correctly-
    # aligned 900).
    from guvcfd.contaminant_source import _source_box

    Lx, Ly, Lz = 4.0, 5.0, 3.0
    cell_size = 0.09
    actual = [Lx / round(Lx / cell_size), Ly / round(Ly / cell_size), Lz / round(Lz / cell_size)]
    lo, hi = _source_box((2.0, 2.5, 1.5), 0.8, cell_size=cell_size, room_dims=(Lx, Ly, Lz))
    for i in range(3):
        assert abs(round(lo[i] / actual[i]) * actual[i] - lo[i]) < 1e-9
        assert abs(round(hi[i] / actual[i]) * actual[i] - hi[i]) < 1e-9
    # And explicitly NOT aligned to the nominal cell_size grid instead -
    # pins the fix against silently reverting to the old (wrong) behavior.
    assert abs(round(lo[0] / cell_size) * cell_size - lo[0]) > 1e-3


def test_source_box_requires_room_dims_when_cell_size_given():
    from guvcfd.contaminant_source import _source_box
    import pytest
    with pytest.raises(ValueError, match="room_dims"):
        _source_box((2.0, 2.5, 1.5), 0.8, cell_size=0.09)


def test_source_topo_set_dict_accepts_scalar_size():
    text = source_topo_set_dict((1.0, 1.0, 1.0), 0.2)
    assert "box     (0.9 0.9 0.9) (1.1 1.1 1.1)" in text


def _fake_wsl_result(stdout):
    return SimpleNamespace(stdout=stdout, returncode=0)


def test_check_mass_balance_within_tolerance(monkeypatch, tmp_path):
    def fake_run_wsl_or_raise(cmd, cwd_wsl, step_name):
        return _fake_wsl_result(
            "sum(outlet) of phi = 0.0102\nweightedAverage(outlet) of T = 1.9394"
        )

    monkeypatch.setattr(contaminant_source, "run_wsl_or_raise", fake_run_wsl_or_raise)
    monkeypatch.setattr(contaminant_source, "run_wsl", lambda cmd, cwd_wsl: _fake_wsl_result(""))

    (tmp_path / "system").mkdir()
    # G = 0.027, measured removal = 0.0102 * 1.9394 = 0.019782 -> ratio ~0.733,
    # well outside a 10% tolerance - this is the still-converging case, not the
    # converged one, so it should be flagged as NOT balanced.
    result = check_mass_balance(str(tmp_path), ("outlet",), injection_rate_G=0.027, tol=0.10,
                                 log_fn=lambda *a: None)
    assert result["within_tolerance"] is False
    assert abs(result["measured_removal_rate"] - 0.0102 * 1.9394) < 1e-9


def test_check_mass_balance_when_actually_balanced(monkeypatch, tmp_path):
    def fake_run_wsl_or_raise(cmd, cwd_wsl, step_name):
        # outlet flow * flow-weighted T == G exactly -> a genuinely
        # converged steady state.
        return _fake_wsl_result(
            "sum(outlet) of phi = 0.027\nweightedAverage(outlet) of T = 1.0"
        )

    monkeypatch.setattr(contaminant_source, "run_wsl_or_raise", fake_run_wsl_or_raise)
    monkeypatch.setattr(contaminant_source, "run_wsl", lambda cmd, cwd_wsl: _fake_wsl_result(""))

    (tmp_path / "system").mkdir()
    result = check_mass_balance(str(tmp_path), ("outlet",), injection_rate_G=0.027, tol=0.10,
                                 log_fn=lambda *a: None)
    assert result["within_tolerance"] is True
    assert abs(result["ratio"] - 1.0) < 1e-9


def test_check_mass_balance_sums_multiple_patches(monkeypatch, tmp_path):
    def fake_run_wsl_or_raise(cmd, cwd_wsl, step_name):
        return _fake_wsl_result(
            "sum(outlet) of phi = 0.015\nweightedAverage(outlet) of T = 1.0\n"
            "sum(outlet2) of phi = 0.012\nweightedAverage(outlet2) of T = 1.0"
        )

    monkeypatch.setattr(contaminant_source, "run_wsl_or_raise", fake_run_wsl_or_raise)
    monkeypatch.setattr(contaminant_source, "run_wsl", lambda cmd, cwd_wsl: _fake_wsl_result(""))

    (tmp_path / "system").mkdir()
    result = check_mass_balance(str(tmp_path), ("outlet", "outlet2"), injection_rate_G=0.027, tol=0.10,
                                 log_fn=lambda *a: None)
    assert abs(result["measured_removal_rate"] - 0.027) < 1e-9


def test_check_mass_balance_raises_on_unparseable_output(monkeypatch, tmp_path):
    monkeypatch.setattr(contaminant_source, "run_wsl_or_raise",
                         lambda cmd, cwd_wsl, step_name: _fake_wsl_result("nothing useful here"))
    monkeypatch.setattr(contaminant_source, "run_wsl", lambda cmd, cwd_wsl: _fake_wsl_result(""))

    (tmp_path / "system").mkdir()
    try:
        check_mass_balance(str(tmp_path), ("outlet",), injection_rate_G=0.027, log_fn=lambda *a: None)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


# --- breathing_inlet_velocity_constraint (the live implementation) ---

def test_breathing_inlet_velocity_constraint_defaults_to_x_direction():
    text = breathing_inlet_velocity_constraint(velocity_magnitude=0.06)
    assert "breathingInletVelocity" in text
    # A CONSTRAINT (setValues replaces the matrix row), not a source that only
    # adds terms and loses to the pressure correction - see the docstring.
    assert "type            vectorFixedValueConstraint;" in text
    assert "cellZone        sourceZone;" in text
    # the raw target velocity, NOT scaled by any coefficient
    assert "U               (6.000000e-02 0.000000e+00 0.000000e+00);" in text


def test_breathing_inlet_velocity_constraint_is_not_a_semi_implicit_source():
    """Guard against regressing to the superseded approach: a source cannot
    dictate a velocity - even with correct coefficients it converged to
    2.25 m/s against a 0.06 m/s target because SIMPLE's pressure correction
    re-solves U afterwards.
    """
    text = breathing_inlet_velocity_constraint()
    assert "SemiImplicitSource" not in text
    assert "injectionRateSuSp" not in text


def test_breathing_inlet_velocity_constraint_uses_given_zone_and_entry_name():
    text = breathing_inlet_velocity_constraint(zone_name="myZone", entry_name="myEntry")
    assert text.startswith("myEntry")
    assert "cellZone        myZone;" in text


def test_breathing_inlet_velocity_constraint_normalizes_a_non_unit_direction():
    # (0, 2, 0) normalizes to (0, 1, 0) - full velocity_magnitude on y only
    text = breathing_inlet_velocity_constraint(velocity_magnitude=0.5, direction=(0, 2, 0))
    assert "U               (0.000000e+00 5.000000e-01 0.000000e+00);" in text


def test_breathing_inlet_velocity_constraint_falls_back_to_x_on_zero_direction():
    text_zero = breathing_inlet_velocity_constraint(velocity_magnitude=0.06, direction=(0, 0, 0))
    text_default = breathing_inlet_velocity_constraint(velocity_magnitude=0.06)
    assert text_zero == text_default


# --- breathing_inlet_momentum_source (SUPERSEDED - kept as a record) ---

def test_breathing_inlet_momentum_source_defaults_to_x_direction():
    text = breathing_inlet_momentum_source(velocity_magnitude=0.06)
    assert "breathingInletMomentum" in text
    assert "type            vectorSemiImplicitSource;" in text
    assert "cellZone        sourceZone;" in text
    # Su = sp_coeff*U_target = 100*0.06 in x, zero in y/z; Sp = -sp_coeff.
    # The NEGATIVE Sp is the whole point: Su + Sp*U = k*(U_target - U) is a
    # restoring drag, while a positive Sp amplifies U instead (the original
    # bug - drove source-zone |U| to 21.6 m/s against a 0.06 m/s target).
    assert "((6.00e+00 0.00e+00 0.00e+00) -1.00e+02);" in text


def test_breathing_inlet_momentum_source_uses_given_zone_and_entry_name():
    text = breathing_inlet_momentum_source(zone_name="myZone", entry_name="myEntry")
    assert text.startswith("myEntry")
    assert "cellZone        myZone;" in text


def test_breathing_inlet_momentum_source_normalizes_a_non_unit_direction():
    # (0, 2, 0) normalizes to (0, 1, 0) - full velocity_magnitude on y only
    text = breathing_inlet_momentum_source(velocity_magnitude=0.5, direction=(0, 2, 0))
    assert "((0.00e+00 5.00e+01 0.00e+00) -1.00e+02);" in text


def test_breathing_inlet_momentum_source_falls_back_to_x_on_zero_direction():
    text_zero = breathing_inlet_momentum_source(velocity_magnitude=0.06, direction=(0, 0, 0))
    text_default = breathing_inlet_momentum_source(velocity_magnitude=0.06)
    assert text_zero == text_default


def test_breathing_inlet_momentum_source_accepts_a_custom_sp_coeff():
    text = breathing_inlet_momentum_source(velocity_magnitude=0.06, sp_coeff=10.0)
    # Su = sp_coeff * velocity_target = 10 * 0.06 = 0.6; Sp = -10
    assert "((6.00e-01 0.00e+00 0.00e+00) -1.00e+01);" in text


def test_breathing_inlet_momentum_source_sp_is_negative_a_drag_not_a_gain():
    """Regression guard for the sign bug: Sp must be NEGATIVE. OpenFOAM adds
    Su + Sp*U, so Sp>0 is positive feedback that amplifies velocity without
    bound instead of relaxing it toward the target - it drove a real run's
    source-zone |U| to 21.6 m/s against a 0.06 m/s target.
    """
    import re
    text = breathing_inlet_momentum_source(velocity_magnitude=0.06, sp_coeff=100.0)
    m = re.search(r"U\s+\(\([^)]*\)\s+(-?[0-9.e+-]+)\);", text)
    assert m, f"could not find the SuSp entry in:\n{text}"
    assert float(m.group(1)) < 0, "Sp must be negative (a drag), not positive (a gain)"
