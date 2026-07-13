from guv_calcs import Project

from guvcfd.app import _estimate_well_mixed_eACH, _settling_iterations

GUV_PATH = r"c:\Users\hukcl\Documents\Python\Illuminator2\illuminate-v4\4x3x2.7.guv"


def _load_room():
    project = Project.load(GUV_PATH)
    return next(iter(project.rooms.values()))


def test_estimate_well_mixed_each_is_positive_and_reasonable():
    room = _load_room()
    eACH = _estimate_well_mixed_eACH(room, z_value=2.0)
    # Real CFD run on this exact room/Z measured 19.6/hr - a coarse grid
    # estimate should land in the same ballpark, not off by orders of magnitude.
    assert 5.0 < eACH < 60.0


def test_estimate_scales_with_z():
    room = _load_room()
    eACH_low = _estimate_well_mixed_eACH(room, z_value=1.0)
    eACH_high = _estimate_well_mixed_eACH(room, z_value=4.0)
    assert eACH_high == eACH_low * 4  # k = Z * E, linear in Z


def test_settling_iterations_respects_custom_bounds():
    # Decay duration field is bounded [10, 7200]s, not the 500/50000 default.
    assert _settling_iterations(1e6, target_fraction=0.99,
                                 min_iterations=10, max_iterations=7200) == 10
    assert _settling_iterations(1e-6, target_fraction=0.99,
                                 min_iterations=10, max_iterations=7200) == 7200
