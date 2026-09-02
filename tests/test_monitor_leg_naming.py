"""postProcess names its output directory after the run's START time, so a
decay that extended itself does not write to "0".

Regression for patient ward 4B1 v9, which finished its solve cleanly and then
failed to summarise with:

    reading .../postProcessing/monitor_Patient/0/volFieldValue.dat via WSL
    failed: [Errno 2] No such file

The decay extension sets `startFrom latestTime` to continue, so the post-hoc
monitoring pass wrote postProcessing/monitor_Patient/5100/ instead. The file
held the COMPLETE t=0..5100 series - only the directory name differed - so
this was a pure reader bug that destroyed a finished multi-hour run's report.
"""
import inspect

import pytest

from guvcfd.decay_analysis import decay_monitor_legs, read_decay_curve

HEADER = ("# Region      : cellZone Patient\n"
          "# Cells       : 12\n"
          "# Time        \tvolAverage(T)\n")


def _write(case, monitor, leg, rows):
    d = case / "postProcessing" / monitor / leg
    d.mkdir(parents=True)
    (d / "volFieldValue.dat").write_text(
        HEADER + "".join(f"{t}\t{v}\n" for t, v in rows))


def test_monitor_leg_named_after_restart_time_is_found(tmp_path):
    """The exact v9 shape: one leg, named 5100, holding the whole series."""
    _write(tmp_path, "monitor_Patient", "5100",
           [(0.0, 1.0), (50.0, 0.94), (5100.0, 0.000672)])
    assert decay_monitor_legs(str(tmp_path), "monitor_Patient") == ["5100"]
    t, T = read_decay_curve(str(tmp_path), monitor="monitor_Patient")
    assert list(t) == [0.0, 50.0, 5100.0]
    assert T[-1] == pytest.approx(0.000672)


def test_zero_leg_still_works(tmp_path):
    """A run that never restarted must behave exactly as before."""
    _write(tmp_path, "monitor_exhaust", "0", [(0.0, 1.0), (100.0, 0.5)])
    t, T = read_decay_curve(str(tmp_path), monitor="monitor_exhaust")
    assert list(t) == [0.0, 100.0]


def test_monitoring_points_does_not_hardcode_leg_zero():
    """Guard the specific line that failed - a reintroduced literal '0' path
    would only surface after a full run, at summarising time."""
    import guvcfd.monitoring_points as mp
    src = inspect.getsource(mp)
    assert "monitor_{zname}/0/volFieldValue.dat" not in src
    assert "read_decay_curve" in src


def test_convergence_monitor_read_is_leg_agnostic():
    """converge_flow_field wipes and regenerates postProcessing each chunk, so
    exactly one leg exists - but its name is the start time, not necessarily
    '0'."""
    import guvcfd.run_pipeline as rp
    src = inspect.getsource(rp.converge_flow_field)
    assert "volAverage1/0/volFieldValue.dat" not in src
    assert "read_decay_curve" in src
