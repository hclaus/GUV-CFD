"""Result keys renamed 2026-09-02, with old files still readable.

"effective" named the value computed against the NOMINAL ventilation rate -
the one that assumes perfect mixing. In a badly mixed room that goes NEGATIVE
and reads as "UV made things worse", when it actually means "ventilation never
did the work that was assumed". patient ward v9 reported
eACH_uv_effective = -0.91 /hr while the room was genuinely being disinfected
at 4.73 /hr. The measured-baseline value (was "_corrected") is the one that is
actually effective, so it is now "_actual".
"""
import json

import pytest

from guvcfd.decay_analysis import LEGACY_RESULT_KEYS, migrate_result_keys


def test_every_old_key_maps_to_a_new_one():
    assert LEGACY_RESULT_KEYS["eACH_uv_effective"] == "eACH_uv_assuming_well_mixed"
    assert LEGACY_RESULT_KEYS["eACH_uv_effective_corrected"] == "eACH_uv_actual"
    # No new name may collide with an old one, or migration would loop.
    assert not (set(LEGACY_RESULT_KEYS) & set(LEGACY_RESULT_KEYS.values()))


def test_an_old_results_file_gains_the_new_keys():
    old = {"eACH_uv_effective": -0.9099, "eACH_uv_effective_corrected": 4.7342,
           "total_ach_effective": 5.0901, "ventilation_ach_measured": 0.3559}
    out = migrate_result_keys(dict(old))
    assert out["eACH_uv_assuming_well_mixed"] == pytest.approx(-0.9099)
    assert out["eACH_uv_actual"] == pytest.approx(4.7342)
    assert out["total_ach_actual"] == pytest.approx(5.0901)
    # old keys are LEFT IN PLACE - a user's own script/CSV may still read them
    assert out["eACH_uv_effective"] == pytest.approx(-0.9099)


def test_a_current_file_is_not_modified():
    cur = {"eACH_uv_assuming_well_mixed": -1.0, "eACH_uv_actual": 4.0}
    assert migrate_result_keys(dict(cur)) == cur


def test_migration_never_overwrites_an_existing_new_key():
    mixed = {"eACH_uv_effective": 1.0, "eACH_uv_assuming_well_mixed": 2.0}
    assert migrate_result_keys(dict(mixed))["eACH_uv_assuming_well_mixed"] == 2.0


def test_no_source_file_still_writes_the_old_names():
    """The writer must emit only current names; the old ones survive solely as
    a read-side compatibility shim."""
    import inspect
    import guvcfd.decay_analysis as da
    src = inspect.getsource(da.write_results_summary)
    for old in LEGACY_RESULT_KEYS:
        assert f'"{old}"' not in src, f"write_results_summary still writes {old}"


def test_non_dict_input_is_returned_unchanged():
    assert migrate_result_keys(None) is None
    assert migrate_result_keys([1, 2]) == [1, 2]


# ---------------------------------------------------------------- error paths
# A rename's happy path is easy; what breaks is a key being ABSENT. Three ways
# that happens for real: (a) a results.json written before the rename, (b) a
# run that legitimately has no control (sealed / mechanical-ACH-only), so
# eACH_uv_actual was never computed, (c) a truncated or corrupt file.

def test_every_external_results_read_migrates():
    """Any function that PARSES a results.json it did not just write must
    migrate it, or a pre-rename file silently loses its values. These are the
    resume / extend / sweep-skip paths - exactly the ones handed old files.

    Matched per-function with AST rather than by substring, so that reads of
    run_settings.json (which has no renamed keys and must NOT be migrated)
    are not flagged.
    """
    import ast
    import importlib
    import inspect

    # The PC fork ships the Qt GUI only and has no dash, so guvcfd.app is
    # genuinely absent there - check the modules that exist on both.
    mods = []
    for name in ("guvcfd.app", "guvcfd.scenario_runs", "guvcfd.report",
                 "guvcfd.qtapp.analysis_tab"):
        try:
            mods.append(importlib.import_module(name))
        except ModuleNotFoundError as exc:
            if name == "guvcfd.app" and "dash" in str(exc):
                continue
            raise
    assert len(mods) >= 3

    offenders = []
    for mod in mods:
        tree = ast.parse(inspect.getsource(mod))
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            body = ast.unparse(fn)
            if "results.json" not in body:
                continue
            parses = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                         and n.func.attr in ("load", "loads")
                         and getattr(n.func.value, "id", "") == "json"
                         for n in ast.walk(fn))
            if parses and "migrate_result_keys" not in body:
                offenders.append(f"{mod.__name__}.{fn.name}")
    # These mention a results.json path but the json they PARSE is
    # run_settings.json, which has no renamed keys and must not be migrated.
    ALLOWED = {"guvcfd.app._open_paraview",
               "guvcfd.scenario_runs.rebuild_project_status_from_disk"}
    offenders = [o for o in offenders if o not in ALLOWED]
    assert not offenders, "parse a results.json without migrating: " + ", ".join(offenders)


def test_missing_optional_keys_do_not_crash_the_summary_metrics():
    """eACH_uv_actual only exists when a control ran. A sealed or
    mechanical-ACH-only run has none, and must still summarise."""
    from guvcfd.decay_analysis import mechanical_mixing_efficiency_pct
    no_control = {"eACH_uv_assuming_well_mixed": -0.9,
                  "ach_delivery": {"measured_ach": 5.98}}
    assert mechanical_mixing_efficiency_pct(no_control) is None      # not a crash
    assert mechanical_mixing_efficiency_pct({}) is None
    assert mechanical_mixing_efficiency_pct({"ventilation_ach_measured": 0.3}) is None


def test_migration_is_idempotent():
    """Resume paths can migrate the same dict more than once."""
    d = {"eACH_uv_effective": 1.0}
    once = migrate_result_keys(dict(d))
    twice = migrate_result_keys(dict(once))
    assert once == twice


def test_migration_tolerates_null_and_missing_values():
    """A run stopped early writes nulls rather than omitting keys."""
    d = {"eACH_uv_effective": None, "eACH_uv_effective_corrected": None}
    out = migrate_result_keys(dict(d))
    assert out["eACH_uv_assuming_well_mixed"] is None
    assert out["eACH_uv_actual"] is None


def test_combo_summary_metrics_survives_an_old_and_a_partial_file():
    """The sweep dashboard reads whatever is on disk, including pre-rename
    and half-written files."""
    from guvcfd.scenario_runs import combo_summary_metrics
    old = migrate_result_keys({"eACH_uv_effective": -0.9,
                               "eACH_uv_effective_corrected": 4.73,
                               "eACH_uv_well_mixed": 72.4})
    m = combo_summary_metrics(old)
    assert m is not None
    assert combo_summary_metrics({}) is not None or combo_summary_metrics({}) is None
