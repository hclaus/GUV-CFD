import math

from guvcfd.contaminant_source import compute_source_strength, source_Su
from guvcfd.steady_state_pipeline import compute_corrected_eACH_uv

ROOM_VOLUME = 30.0
ACH = 3.0
TARGET_T_SS = 0.3
SOURCE_VOLUME = 1.0


def _setup():
    G = compute_source_strength(ROOM_VOLUME, ACH, TARGET_T_SS)
    Su = source_Su(G, SOURCE_VOLUME)
    return Su


def test_matches_nominal_when_phase1_lands_exactly_on_target():
    # If Phase 1's real steady state lands exactly on the idealized target
    # (perfect well-mixed ventilation), the measured rate must equal nominal.
    Su = _setup()
    T_ss1 = TARGET_T_SS
    T_ss2 = 0.05
    ventilation_ach_measured, eACH_corrected = compute_corrected_eACH_uv(
        T_ss1, T_ss2, Su, SOURCE_VOLUME, ROOM_VOLUME)
    assert math.isclose(ventilation_ach_measured, ACH, rel_tol=1e-9)

    lambda_vent_nominal = ACH / 3600.0
    eACH_uncorrected = lambda_vent_nominal * (T_ss1 / T_ss2 - 1) * 3600
    assert math.isclose(eACH_corrected, eACH_uncorrected, rel_tol=1e-9)


def test_higher_than_target_T_ss1_means_lower_measured_ventilation():
    # Real T_ss1 landing ABOVE the idealized target means ventilation is
    # removing contaminant less effectively than the nominal ACH assumes.
    Su = _setup()
    ventilation_ach_measured, _ = compute_corrected_eACH_uv(
        0.4, 0.05, Su, SOURCE_VOLUME, ROOM_VOLUME)
    assert ventilation_ach_measured < ACH
    assert math.isclose(ventilation_ach_measured, ACH * (TARGET_T_SS / 0.4), rel_tol=1e-9)


def test_lower_than_target_T_ss1_means_higher_measured_ventilation():
    Su = _setup()
    ventilation_ach_measured, _ = compute_corrected_eACH_uv(
        0.2, 0.05, Su, SOURCE_VOLUME, ROOM_VOLUME)
    assert ventilation_ach_measured > ACH
    assert math.isclose(ventilation_ach_measured, ACH * (TARGET_T_SS / 0.2), rel_tol=1e-9)


def test_returns_none_for_zero_T_ss():
    Su = _setup()
    assert compute_corrected_eACH_uv(0.0, 0.05, Su, SOURCE_VOLUME, ROOM_VOLUME) == (None, None)
    assert compute_corrected_eACH_uv(0.3, 0.0, Su, SOURCE_VOLUME, ROOM_VOLUME) == (None, None)
