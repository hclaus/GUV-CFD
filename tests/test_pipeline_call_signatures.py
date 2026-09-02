"""Every keyword passed to a pipeline entry point must exist in its signature.

A blanket text edit threading `converged_chunks` through the flow-convergence
call sites landed it on setup_case()/resume_case_setup() calls too - wrappers
that forward to converge_flow_field() but did not themselves accept it. The
whole suite (785 tests) stayed green because nothing binds these calls; the
failure only appeared at runtime, as

    setup_case() got an unexpected keyword argument 'converged_chunks'

which costs a full run to discover. This checks the binding statically.
"""
import ast
import inspect
import importlib

import pytest

# Entry points worth guarding: multi-parameter pipeline functions called from
# several GUIs, where a kwarg typo cannot fail until a real run is underway.
TARGETS = [
    "setup_case", "resume_case_setup", "converge_flow_field",
    "continue_flow_convergence", "case_awaiting_flow_decision",
    "prepare_ventilation_only_control",
]
CALLERS = ["guvcfd.app", "guvcfd.qtapp.run_state", "guvcfd.scenario_runs",
           "guvcfd.run_pipeline", "guvcfd.steady_state_pipeline"]


def _signatures():
    import guvcfd.run_pipeline as rp
    import guvcfd.ventilation_control as vc
    sigs = {}
    for name in TARGETS:
        fn = getattr(rp, name, None) or getattr(vc, name, None)
        assert fn is not None, f"{name} not found - test needs updating"
        sigs[name] = inspect.signature(fn)
    return sigs


def _accepts(sig, kw):
    if kw in sig.parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


@pytest.mark.parametrize("module_name", CALLERS)
def test_every_keyword_argument_is_accepted(module_name):
    try:
        mod = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if module_name == "guvcfd.app" and "dash" in str(exc):
            pytest.skip("PC fork has no dash")
        raise
    sigs = _signatures()
    tree = ast.parse(inspect.getsource(mod))
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else (
            fn.attr if isinstance(fn, ast.Attribute) else None)
        if name not in sigs:
            continue
        for kw in node.keywords:
            if kw.arg is None:          # **kwargs splat - cannot check statically
                continue
            checked += 1
            assert _accepts(sigs[name], kw.arg), (
                f"{module_name}:{node.lineno} passes {kw.arg}= to {name}(), "
                f"which does not accept it")
    # Guard the guard: if the walk stops finding calls, the test is vacuous.
    if module_name in ("guvcfd.run_pipeline", "guvcfd.qtapp.run_state"):
        assert checked > 0, f"no {TARGETS} calls found in {module_name}"


def test_no_call_site_still_passes_the_removed_converged_chunks():
    """converged_chunks was the streak criterion's knob. The streak test was
    replaced by the window-based one (see test_flow_convergence_criterion),
    so the parameter is gone - a leftover caller would now raise the very
    TypeError this module exists to prevent."""
    import guvcfd.run_pipeline as rp
    import guvcfd.qtapp.run_state as rs
    for mod in (rp, rs):
        assert "converged_chunks" not in inspect.getsource(mod),             f"{mod.__name__} still references the removed converged_chunks"
    for fn in (rp.setup_case, rp.resume_case_setup, rp.converge_flow_field,
               rp.continue_flow_convergence):
        assert "converged_chunks" not in inspect.signature(fn).parameters


def test_the_acceptance_helpers_are_importable_and_wired():
    """The two window tests acceptance now depends on must exist and be used
    inside converge_flow_field - not merely defined."""
    import guvcfd.run_pipeline as rp
    src = inspect.getsource(rp.converge_flow_field)
    for name in ("window_mean_settled", "mean_is_stationary", "_is_stable_oscillation"):
        assert callable(getattr(rp, name)), name
        assert name in src, f"{name} is defined but not used by converge_flow_field"
