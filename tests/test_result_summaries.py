from guvcfd.app import _decay_summary, _steady_state_summary


def _flatten_text(node):
    if isinstance(node, str):
        return node
    children = getattr(node, "children", None)
    if children is None:
        return ""
    if isinstance(children, (list, tuple)):
        return "".join(_flatten_text(c) for c in children)
    return _flatten_text(children)


def _all_text(components):
    return "\n".join(_flatten_text(c) for c in components)


_DECAY_RESULT = {
    "ventilation_ach": 3.0, "eACH_uv_well_mixed": 10.27, "eACH_uv_effective": 8.97,
    "mixing_efficiency": 0.873, "total_ach_effective": 11.97,
    "decay_curve": {"t_seconds": [0, 10], "volAverage_T": [1.0, 0.9]},
    "fluence_mean": 5.678,
}

_STEADY_STATE_RESULT = {
    "target_T_ss": 0.3,
    "phase1": {"T_ss": 0.2548, "converged": True, "iterations": 8000},
    "phase2": {"T_ss": 0.0644, "converged": False, "iterations": 3000},
    "reduction_pct": 74.7,
    "eACH_uv_steady_state": 17.73,
    "fluence_mean": 12.34,
    "injection_rate_total": 0.598,
}


def test_decay_summary_labels_state_what_each_eACH_is():
    text = _all_text(_decay_summary(_DECAY_RESULT))
    assert "eACH_uv, well-mixed (idealized: Z x E_avg)" in text
    assert "eACH_uv, CFD-fit (nominal ventilation ACH)" in text


def test_decay_summary_corrected_row_names_the_correction():
    result = dict(_DECAY_RESULT)
    result["ventilation_ach_measured"] = 2.8
    result["eACH_uv_effective_corrected"] = 9.5
    result["mixing_efficiency_corrected"] = 0.9
    text = _all_text(_decay_summary(result))
    assert "eACH_uv, CFD-fit (measured ventilation ACH)" in text
    assert "9.5" in text


def test_decay_summary_always_includes_t_field_note():
    text = _all_text(_decay_summary(_DECAY_RESULT))
    assert "T is the OpenFOAM field name" in text


def test_steady_state_summary_includes_injection_rate():
    text = _all_text(_steady_state_summary(_STEADY_STATE_RESULT))
    assert "Source injection rate" in text
    assert "0.598" in text


def test_steady_state_summary_omits_injection_rate_when_absent():
    result = dict(_STEADY_STATE_RESULT)
    del result["injection_rate_total"]
    text = _all_text(_steady_state_summary(result))
    assert "Source injection rate" not in text


def test_steady_state_summary_corrected_row_names_the_correction():
    result = dict(_STEADY_STATE_RESULT)
    result["ventilation_ach_measured"] = 2.55
    result["eACH_uv_steady_state_corrected"] = 18.1
    text = _all_text(_steady_state_summary(result))
    assert "eACH_uv, steady-state CFD-fit (measured ventilation ACH)" in text


def test_steady_state_summary_nominal_row_points_at_corrected_row_when_present():
    # The "nominal" eACH_uv row plugs in the design ACH, not a measured
    # one - when a measured/corrected row also exists, its label must say
    # so explicitly (this assumption is often wrong - see the measured
    # row), not just say "nominal ventilation ACH" as if that were a
    # trustworthy result on its own.
    result = dict(_STEADY_STATE_RESULT)
    result["ventilation_ach_measured"] = 2.55
    result["eACH_uv_steady_state_corrected"] = 18.1
    text = _all_text(_steady_state_summary(result))
    assert "assumes nominal design ACH" in text
    assert "see measured-ACH row below" in text


def test_steady_state_summary_nominal_row_label_is_plain_when_no_corrected_row():
    text = _all_text(_steady_state_summary(_STEADY_STATE_RESULT))
    assert "assumes nominal design ACH)" in text
    assert "see measured-ACH row below" not in text


def test_steady_state_summary_shows_moving_average_and_cv_when_present():
    result = dict(_STEADY_STATE_RESULT)
    result["phase1"] = dict(result["phase1"], T_ss_std=0.003, T_ss_cv=0.012, T_ss_window_span=1234)
    result["phase2"] = dict(result["phase2"], T_ss_std=0.0009, T_ss_cv=0.014, T_ss_window_span=456)
    text = _all_text(_steady_state_summary(result))
    assert "Phase 1 moving average (no UV, last 1234 iterations)" in text
    assert "Phase 1 CV (no UV, last 1234 iterations)" in text
    assert "1.2%" in text
    assert "Phase 2 moving average (UV on, last 456 iterations)" in text
    assert "1.4%" in text


def test_steady_state_summary_falls_back_to_plain_t_ss_when_window_fields_absent():
    # Old results.json predating live-volAverage tracking has no
    # T_ss_window_span - must show the old plain-T_ss row, not crash.
    text = _all_text(_steady_state_summary(_STEADY_STATE_RESULT))
    assert "Phase 1 T_ss" in text
    assert "moving average" not in text
    assert "CV (last" not in text


def test_steady_state_summary_shows_extrapolated_t_infinity_when_present():
    result = dict(_STEADY_STATE_RESULT)
    result["phase1"] = dict(result["phase1"], T_ss_cv=0.012, T_ss_window_span=1234,
                             T_inf_extrapolated=0.271, T_inf_extrapolation_detail={"fit_cv": 0.0008})
    result["phase2"] = dict(result["phase2"], T_ss_cv=0.014, T_ss_window_span=456,
                             T_inf_extrapolated=0.061, T_inf_extrapolation_detail={"fit_cv": 0.0012})
    text = _all_text(_steady_state_summary(result))
    assert "Phase 1 extrapolated T∞ (no UV, n→∞)" in text
    assert "0.271" in text
    assert "Phase 2 extrapolated T∞ (UV on, n→∞)" in text
    assert "0.061" in text


def test_steady_state_summary_omits_extrapolated_t_infinity_when_absent():
    result = dict(_STEADY_STATE_RESULT)
    result["phase1"] = dict(result["phase1"], T_ss_cv=0.012, T_ss_window_span=1234)
    result["phase2"] = dict(result["phase2"], T_ss_cv=0.014, T_ss_window_span=456)
    text = _all_text(_steady_state_summary(result))
    assert "extrapolated T∞" not in text


def test_steady_state_summary_notes_ach_source_when_extrapolated():
    result = dict(_STEADY_STATE_RESULT)
    result["ach_source"] = "extrapolated_T_infinity"
    result["ventilation_ach_measured"] = 3.2
    result["eACH_uv_steady_state_corrected"] = 40.1
    text = _all_text(_steady_state_summary(result))
    assert "using extrapolated T∞" in text


def test_steady_state_summary_notes_ach_source_when_windowed():
    result = dict(_STEADY_STATE_RESULT)
    result["ach_source"] = "windowed_average"
    text = _all_text(_steady_state_summary(result))
    assert "T∞ extrapolation unavailable" in text


def test_steady_state_summary_no_ach_source_note_for_old_results():
    # Predates the ach_source field entirely - must not crash or show a
    # note that doesn't apply.
    text = _all_text(_steady_state_summary(_STEADY_STATE_RESULT))
    assert "extrapolated T∞" not in text
    assert "T∞ extrapolation unavailable" not in text


def test_steady_state_summary_flags_non_uniform_mixing():
    result = dict(_STEADY_STATE_RESULT)
    result["monitoring"] = {
        "Patient": {
            "phase1": {"volAverage_T": [0.0, 0.1957]},
            "phase2": {"volAverage_T": [0.1957, 0.0122]},
        },
    }
    text = _all_text(_steady_state_summary(result))
    assert "NOT well mixed" in text
