import math

from guvcfd.app import _decay_figure


def _sample_result():
    t = [0.0, 10.0, 20.0, 30.0]
    T = [1.0, 0.9, 0.81, 0.729]  # pure exp(-0.01054*t) decay, for a clean check
    return {
        "ventilation_ach": 3.0,
        "eACH_uv_well_mixed": 5.0,
        "decay_curve": {"t_seconds": t, "volAverage_T": T},
    }


def test_log_y_axis():
    fig = _decay_figure(_sample_result())
    assert fig.layout.yaxis.type == "log"


def test_three_traces_present():
    fig = _decay_figure(_sample_result())
    names = [tr.name for tr in fig.data]
    assert len(names) == 3
    assert any("actual" in n for n in names)
    assert any("Ventilation ACH only" in n for n in names)
    assert any("Well-mixed" in n for n in names)


def test_reference_curves_start_at_T0_and_decay_at_expected_rate():
    result = _sample_result()
    fig = _decay_figure(result)
    ach_trace = next(tr for tr in fig.data if "Ventilation ACH only" in tr.name)
    well_mixed_trace = next(tr for tr in fig.data if "Well-mixed" in tr.name)

    T0 = result["decay_curve"]["volAverage_T"][0]
    assert ach_trace.y[0] == T0
    assert well_mixed_trace.y[0] == T0

    t_last = result["decay_curve"]["t_seconds"][-1]
    lambda_vent = result["ventilation_ach"] / 3600.0
    lambda_well_mixed = lambda_vent + result["eACH_uv_well_mixed"] / 3600.0
    assert math.isclose(ach_trace.y[-1], T0 * math.exp(-lambda_vent * t_last), rel_tol=1e-9)
    assert math.isclose(well_mixed_trace.y[-1], T0 * math.exp(-lambda_well_mixed * t_last), rel_tol=1e-9)
    # UV's extra removal makes the well-mixed reference decay faster than ACH alone.
    assert well_mixed_trace.y[-1] < ach_trace.y[-1]
