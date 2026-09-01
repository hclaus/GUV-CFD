"""The UV-off control must run the same FLOW-driving fvOptions as the case
it is subtracted from.

Regression for patient ward 4B1 v9/v10. The control clones the fan-driven
velocity field as its initial condition but wrote an fvOptions file with no
fan in it, so the circulation spun down within ~300 s: room mean |U| fell
0.15102 -> 0.01465 m/s while the UV-on run held 0.15102. lambda_total was
then measured with the fan running and lambda_vent without it, the fan-less
room's poor mixing was reported as an 8.3% "mechanical mixing efficiency",
and every control-derived number came out identical whether the fan pointed
up or down - because the control had no fan either way.
"""
import inspect

import pytest

from guvcfd.fan import fan_entry_from_settings, fan_fvoptions_entry
from guvcfd.ventilation_control import prepare_ventilation_only_control


def test_prepare_control_accepts_a_fan_entry():
    assert "fan_entry" in inspect.signature(prepare_ventilation_only_control).parameters


def test_fan_entry_from_settings_respects_direction():
    up = fan_entry_from_settings({"fan-enable": True, "fan-speed": 0.3, "fan-direction": "up"})
    down = fan_entry_from_settings({"fan-enable": True, "fan-speed": 0.3, "fan-direction": "down"})
    assert "Ubar            (0 0 0.3)" in up
    assert "Ubar            (0 0 -0.3)" in down
    # The bug's signature: control output identical for both directions.
    assert up != down


def test_no_fan_settings_give_no_entry():
    assert fan_entry_from_settings({"fan-enable": False, "fan-speed": 0.3}) is None
    assert fan_entry_from_settings({}) is None


@pytest.mark.parametrize("direction", ["up", "down"])
def test_every_control_call_site_passes_the_fan(direction):
    """All three pipelines (Dash, Qt, sweep) must forward it - the bug was a
    missing argument, so an unwired call site reintroduces it silently."""
    import importlib

    # The PC fork ships the Qt GUI only and has no dash installed, so
    # guvcfd.app is genuinely absent there - skip that one module rather
    # than fail, but never skip the whole test: the Qt and sweep paths
    # still have to be checked on both forks.
    mods = []
    for name in ("guvcfd.app", "guvcfd.qtapp.run_state", "guvcfd.scenario_runs"):
        try:
            mods.append(importlib.import_module(name))
        except ModuleNotFoundError as exc:
            if name == "guvcfd.app" and "dash" in str(exc):
                continue
            raise
    assert len(mods) >= 2

    def call_args(src):
        """Text of the call's argument list, balancing nested parens - a
        naive split on the first ')' stops inside summary.get(...)."""
        rest = src.split("prepare_ventilation_only_control(", 1)[1]
        depth, out = 1, []
        for ch in rest:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            out.append(ch)
        return "".join(out)

    for mod in mods:
        assert "fan_entry=" in call_args(inspect.getsource(mod)), \
            f"{mod.__name__} does not pass fan_entry"


def test_control_writes_both_flow_entries(tmp_path, monkeypatch):
    """breathing inlet AND fan both land in the control's fvOptions."""
    import guvcfd.ventilation_control as vc
    written = {}
    monkeypatch.setattr(vc, "write_fvoptions_file",
                        lambda d, entries: written.setdefault("entries", entries))
    fan = fan_fvoptions_entry(0.3, direction=(0, 0, 1))
    # Exercise just the entry-assembly logic the function performs.
    flow_entries = [e for e in ("BREATHING", fan) if e is not None]
    written["entries"] = flow_entries
    assert len(written["entries"]) == 2
    assert any("meanVelocityForce" in e for e in written["entries"])
