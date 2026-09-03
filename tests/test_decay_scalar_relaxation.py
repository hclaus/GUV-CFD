"""T must be solved essentially unrelaxed in a transient (decay) run.

Under-relaxation is a STEADY-state convergence device. In a transient run the
ddt term already supplies that stability, so relaxing T stabilises nothing -
it only stops each timestep reaching the implicit solution, so every sink
term (the UV kill in particular) is applied at a fraction of its strength,
every step, cumulatively.

Measured on patient ward 4B1 v9 (Z=7, kUV.max 3.03/s, identical mesh, flow
and UV sources; relaxation the only variable, 400 s each):

    T 0.05 (as shipped)   ->  4.80 /hr
    T 1.0                 -> 70.74 /hr
    well-mixed prediction -> 72.17 /hr

A 14.7x error, all of it understated UV performance. The room was decaying
slower than its own weakest cell (10.75 /hr), which is impossible for a pure
sink and was the clue.

`TFinal 1` is NOT an alternative: scalarTransport is a function object outside
the PIMPLE outer loop, so finalIteration is never set for it and TFinal is
never consulted. Confirmed - T 0.05 with and without TFinal 1 produced
byte-identical curves (both 4.80 /hr, T 0.9913 -> 0.58157).
"""
import ast
import inspect

import pytest

from guvcfd.app_settings import ADVANCED_SETTINGS_DEFAULTS, PROJECT_OPENFOAM_SETTINGS_KEYS


def test_decay_relaxation_defaults_to_unrelaxed():
    assert ADVANCED_SETTINGS_DEFAULTS["decay-scalar-relaxation"] == 1.0


def test_it_is_separate_from_the_steady_state_value():
    """Phase 2 genuinely needs a low value at high Z; decay must not inherit
    it. They are different settings for different solvers."""
    assert "scalar-relaxation" in ADVANCED_SETTINGS_DEFAULTS
    assert "decay-scalar-relaxation" in ADVANCED_SETTINGS_DEFAULTS
    assert ADVANCED_SETTINGS_DEFAULTS["scalar-relaxation"] != \
        ADVANCED_SETTINGS_DEFAULTS["decay-scalar-relaxation"]


def test_it_is_saved_per_project():
    assert "decay-scalar-relaxation" in PROJECT_OPENFOAM_SETTINGS_KEYS


@pytest.mark.parametrize("modname", ["guvcfd.app", "guvcfd.qtapp.run_state", "guvcfd.scenario_runs"])
def test_every_decay_path_applies_it(modname):
    """All three pipelines must set it. A path that forgets silently returns
    a ~15x-understated eACH - no error, just a wrong number."""
    import importlib
    try:
        mod = importlib.import_module(modname)
    except ModuleNotFoundError as exc:
        if modname == "guvcfd.app" and "dash" in str(exc):
            pytest.skip("PC fork has no dash")
        raise
    src = inspect.getsource(mod)
    # .get(..., 1.0) rather than [..] so an older settings dict cannot crash
    # the run; 1.0 is the correct value regardless.
    assert "decay-scalar-relaxation" in src, f"{modname} never reads the setting"
    assert "set_relaxation_factors" in src, f"{modname} never applies it"


def test_the_control_run_sets_it_too():
    """The sweep's shared control is cloned from a flow-only base that never
    went through decay setup, so it must set its own."""
    import guvcfd.ventilation_control as vc
    sig = inspect.signature(vc.prepare_ventilation_only_control)
    assert "scalar_relaxation" in sig.parameters
    assert sig.parameters["scalar_relaxation"].default == 1.0
    assert "set_relaxation_factors" in inspect.getsource(vc.prepare_ventilation_only_control)


def test_decay_setting_is_applied_after_apply_z_in_the_sweep():
    """_apply_z writes the STEADY value; the decay override must come after it
    or the sweep silently keeps the wrong one."""
    import guvcfd.scenario_runs as sr
    src = inspect.getsource(sr)
    lines = src.splitlines()
    apply_z = [i for i, l in enumerate(lines) if "z_summary = _apply_z(" in l]
    decay = [i for i, l in enumerate(lines) if "result = _run_decay_scenario(" in l]
    assert apply_z and decay
    assert min(decay) > min(apply_z), "_run_decay_scenario must run after _apply_z"
