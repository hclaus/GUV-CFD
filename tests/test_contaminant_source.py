from guvcfd.contaminant_source import source_topo_set_dict


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
