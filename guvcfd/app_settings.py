"""Advanced/expert tunables that apply across every project (unlike
run_settings.json, which is per-.guvcfd-project) - persisted in a single
fixed-name JSON file at the GUV-CFD repo root, edited via the Settings
menu (right of File) and auto-reloaded fresh at the start of every run,
so a change takes effect immediately without restarting the app.
"""
import json
from pathlib import Path

ADVANCED_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "advanced_settings.json"

# rel_tol values are stored as percentages (e.g. 1.0 = 1%) - what the
# Settings UI shows and edits - not the 0.0-1.0 fraction the pipeline
# functions themselves take; divide by 100 at the call site.
ADVANCED_SETTINGS_DEFAULTS = {
    "flow-rel-tol": 1.0,       # % - converge_flow_field's rel_tol
    "flow-max-iterations": 20000,  # hard cap on total flow-convergence iterations
    "plateau-rel-tol": 1.0,    # % - steady-state phase plateau rel_tol
    "plateau-window": 5,       # samples
    "pimple-delta-t": 0.5,     # seconds - decay solver time step
    "mesh-cell-size": 0.10,    # meters
    "uv-zone-bins": 25,        # bins
    "source-zone-size": 0.30,  # meters
}


def load_advanced_settings():
    """The saved advanced settings, backfilling any field missing from an
    older/partial file (or a missing file entirely) with its default -
    never raises, and the returned dict always has every key.
    """
    saved = {}
    if ADVANCED_SETTINGS_PATH.exists():
        try:
            with open(ADVANCED_SETTINGS_PATH) as f:
                saved = json.load(f)
        except (json.JSONDecodeError, OSError):
            saved = {}
    return {k: saved.get(k, default) for k, default in ADVANCED_SETTINGS_DEFAULTS.items()}


def save_advanced_settings(settings):
    """Writes exactly the known keys (ignores anything extra the caller
    passed) so a stray/renamed field can never get permanently baked in.
    """
    to_save = {k: settings.get(k, default) for k, default in ADVANCED_SETTINGS_DEFAULTS.items()}
    with open(ADVANCED_SETTINGS_PATH, "w") as f:
        json.dump(to_save, f, indent=2)
    return to_save
