import json

from guvcfd import app as guvcfd_app


def _make_case_dir(tmp_path, prior):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "results.json").write_text(json.dumps(prior))
    return str(case_dir)


class _FakeStreamResult:
    returncode = 0
    stdout = ""


def test_continue_decay_reads_back_via_the_post_hoc_path_it_wrote(tmp_path, monkeypatch):
    # Regression guard (2026-08-10): _continue_decay runs a POST-HOC
    # `postProcess -dict system/volAverageDict` (writing to
    # postProcessing/volAverage1/0/...), but write_results_summary's own
    # default vol_average_dat points at the LIVE path
    # (postProcessing/volAverageLive1/...) a normal run's own solve writes
    # as it goes - without an explicit override, Continue would silently
    # read a stale/absent live file instead of the curve it just
    # recomputed.
    prior = {"ventilation_ach": 3.0, "eACH_uv_well_mixed": 5.0}
    case_dir = _make_case_dir(tmp_path, prior)

    monkeypatch.setattr(guvcfd_app, "wsl_path", lambda p: p)
    monkeypatch.setattr(guvcfd_app, "set_control_dict_start_from", lambda *a, **k: None)
    monkeypatch.setattr(guvcfd_app, "set_control_dict_time", lambda *a, **k: None)
    monkeypatch.setattr(guvcfd_app, "run_wsl_streaming", lambda *a, **k: _FakeStreamResult())
    monkeypatch.setattr(guvcfd_app, "run_wsl_or_raise", lambda *a, **k: None)
    monkeypatch.setattr(guvcfd_app, "read_latest_time_field", lambda *a, **k: [1.0, 1.0])
    monkeypatch.setattr(guvcfd_app, "spatial_coefficient_of_variation", lambda values: 0.0)
    monkeypatch.setattr(guvcfd_app, "_should_stop", lambda: False)
    monkeypatch.setattr(guvcfd_app, "_complete_all_steps", lambda: None)
    monkeypatch.setattr(guvcfd_app, "_run_log", lambda msg: None)

    captured = {}

    def fake_write_results_summary(case_dir_, out_path, ventilation_ach, well_mixed_eACH_mean,
                                    vol_average_dat=None, **kwargs):
        captured["vol_average_dat"] = vol_average_dat
        return {"eACH_uv_effective": 1.0, "eACH_uv_well_mixed": well_mixed_eACH_mean}

    monkeypatch.setattr(guvcfd_app, "write_results_summary", fake_write_results_summary)

    guvcfd_app._continue_decay(case_dir, end_time=600, write_interval=10)

    assert captured["vol_average_dat"] == "postProcessing/volAverage1/0/volFieldValue.dat"


def test_continue_decay_raises_when_no_prior_results(tmp_path):
    case_dir = tmp_path / "empty_case"
    case_dir.mkdir()
    try:
        guvcfd_app._continue_decay(str(case_dir), end_time=600, write_interval=10)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "results.json" in str(e)
