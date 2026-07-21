from types import SimpleNamespace

import guvcfd.contaminant_source as contaminant_source
from guvcfd.contaminant_source import check_mass_balance, source_topo_set_dict


def test_source_topo_set_dict_no_snap_by_default():
    text = source_topo_set_dict((2.0, 1.5, 1.35), (0.3, 0.3, 0.3))
    assert "box     (1.85 1.35 1.2) (2.15 1.65 1.5)" in text


def test_source_topo_set_dict_snaps_edges_when_cell_size_given():
    # Same center/size as the no-snap case above - the raw box edges
    # (1.85/2.15 etc) sit almost exactly on a cell_size=0.1 grid line, a
    # boxToCell floating-point boundary tie. Snapped edges must instead be
    # exact multiples of cell_size.
    text = source_topo_set_dict((2.0, 1.5, 1.35), (0.3, 0.3, 0.3), cell_size=0.1)
    import re
    m = re.search(r"box\s+\(([^)]*)\)\s+\(([^)]*)\)", text)
    lo = [float(v) for v in m.group(1).split()]
    hi = [float(v) for v in m.group(2).split()]
    for v in lo + hi:
        assert abs(round(v / 0.1) * 0.1 - v) < 1e-9


def test_source_topo_set_dict_snap_never_collapses_to_zero_width():
    text = source_topo_set_dict((2.0, 1.5, 1.35), (0.02, 0.02, 0.02), cell_size=0.1)
    import re
    m = re.search(r"box\s+\(([^)]*)\)\s+\(([^)]*)\)", text)
    lo = [float(v) for v in m.group(1).split()]
    hi = [float(v) for v in m.group(2).split()]
    for l, h in zip(lo, hi):
        assert h - l >= 0.1 - 1e-9  # float roundoff, e.g. 1.4-1.3 == 0.09999999999999998


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
