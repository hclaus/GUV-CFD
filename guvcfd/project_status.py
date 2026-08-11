"""Project-level status tracking: which Z/ACH combinations have already
been run, with what settings, so a later "add more combinations" sweep
can skip ones that are still valid instead of blindly redoing them.

Two independent fingerprints per combo (see the 2026-08-10 project
discussion this implements):

- flow_fingerprint: hash of the mesh/flow/ventilation-affecting settings
  (room geometry, inlet/outlet, ACH, mesh cell size, relaxation, fan) -
  if this matches an existing combo sharing the same ACH, the flow base +
  UV-off control run are safely reusable, regardless of what the .guv
  file's lamp configuration is.

- uv_fingerprint: hash of the computed 0/kUV field - the literal, derived
  per-cell reaction-rate field the solver actually consumes - rather than
  a hash of the raw .guv file itself. Comparing the derived field
  sidesteps any risk of missing a relevant .guv field in a hand-picked
  subset (the concern that ruled out hashing .guv directly), and also
  correctly treats two different .guv files that happen to produce the
  same fluence field as equivalent, which a raw-file comparison would get
  wrong in the stricter direction.

This module only writes/reads the status file - it does NOT yet decide
whether to skip a combo based on these fingerprints (that's the "add
sweeps" GUI/flow work, deliberately a separate follow-up).
"""
import hashlib
import json
import threading
import time
from pathlib import Path

from .case_io import read_openfoam_scalar_field
from .wsl_utils import read_case_file, write_case_file

# The mesh/flow/ventilation-affecting settings subset - deliberately
# excludes anything UV/lamp-related (that's uv_fingerprint's job) and
# anything that only affects HOW a fresh convergence would be judged done
# (flow-rel-tol, flow-max-iterations) rather than what mesh/BCs actually
# produced an existing result.
FLOW_FINGERPRINT_FIELDS = (
    "inlet-wall", "inlet-y-input", "inlet-z-input", "inlet-size-w", "inlet-size-h", "inlet-diffuser-type",
    "inlet2-enable", "inlet2-wall", "inlet2-y-input", "inlet2-z-input", "inlet2-size-w", "inlet2-size-h",
    "inlet2-diffuser-type",
    "outlet-wall", "outlet-y-input", "outlet-z-input", "outlet-size-w", "outlet-size-h",
    "outlet2-enable", "outlet2-wall", "outlet2-y-input", "outlet2-z-input", "outlet2-size-w", "outlet2-size-h",
    "ach",
    "mesh-cell-size", "momentum-relaxation", "scalar-relaxation",
    "fan-enable", "fan-speed", "fan-direction", "fan-radius", "fan-thickness",
    "fan-x-input", "fan-y-input", "fan-z-input",
)


def _stable_hash(parts):
    """A short, stable hex digest of an ordered list of strings - 16 hex
    chars is far more than enough collision resistance for this purpose
    (comparing a handful of combos within one project) while keeping the
    status file's own JSON readable by a human, which matters given the
    "a user can manually get an overview" requirement this file exists
    for in the first place.
    """
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def compute_flow_fingerprint(settings, room):
    """Hash of everything that determines whether an existing flow base +
    UV-off control run can be reused for a new combo sharing the same
    ACH - see FLOW_FINGERPRINT_FIELDS for exactly what's included/excluded
    and why.
    """
    parts = [f"room={room.x:.6g},{room.y:.6g},{room.z:.6g}"]
    parts += [f"{key}={settings.get(key)}" for key in FLOW_FINGERPRINT_FIELDS]
    return _stable_hash(parts)


def compute_uv_fingerprint(case_dir):
    """Hash of the computed 0/kUV field for an already-_apply_z'd case_dir
    - see module docstring for why this (the derived field the solver
    actually consumes) is compared instead of the raw .guv file. Values
    are rounded to 8 significant figures before hashing so harmless
    floating-point noise (e.g. a different summation order between two
    otherwise-identical computations) doesn't cause a spurious mismatch.
    """
    k_values = read_openfoam_scalar_field(f"{case_dir}/0/kUV")
    parts = [f"{v:.8g}" for v in k_values]
    return _stable_hash(parts)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


_status_lock = threading.Lock()


def _status_relative_path(project_name):
    return f"{project_name}_status.json"


def load_project_status(project_dir, project_name, guv_path=None, settings_path=None, sim_type=None):
    """The project's status dict, backfilled with an empty skeleton if the
    file doesn't exist yet or can't be parsed - never raises. Existing
    top-level fields already on disk are never overwritten by the
    backfill defaults here (mirrors capture_openfoam_settings' own
    "never overwrite what's already there" contract).
    """
    try:
        content = read_case_file(project_dir, _status_relative_path(project_name))
        status = json.loads(content)
    except Exception:
        status = {}
    status.setdefault("guv_path", guv_path)
    status.setdefault("settings_path", settings_path)
    status.setdefault("case_dir", project_dir)
    status.setdefault("sim_type", sim_type)
    status.setdefault("combos", {})
    return status


def save_project_status(project_dir, project_name, status):
    status["last_updated"] = now_iso()
    write_case_file(project_dir, _status_relative_path(project_name), json.dumps(status, indent=2))


def _combo_key(z, ach):
    return f"Z{z:g}_ACH{ach:g}"


def _ach_label(ach):
    """Folder-name-safe label for an ACH value - matches
    scenario_runs._ach_label exactly (not imported from there - importing
    scenario_runs here would be circular, since scenario_runs already
    imports this module) so ach_bases keys line up with the actual
    _base_ACH{label}/_control_ACH{label} directory names on disk.
    """
    return "sealed" if ach <= 0 else f"{ach:g}"


def update_combo_status(project_dir, project_name, z, ach, guv_path=None, settings_path=None, sim_type=None,
                         **fields):
    """Read-modify-write a single combo's entry - creates the status file
    (and the combo's own entry within it) if either is missing.

    Guarded by a process-wide lock: sweep combinations run concurrently
    (see scenario_runs._run_sweep_concurrent, up to _MAX_CONCURRENT_Z at
    once) and each calls this independently on the SAME project-level
    file - without a lock, two threads' read-modify-write cycles could
    race (both read before either writes, so the second write silently
    discards the first thread's update). A single global lock is
    sufficient for the current concurrency model (one sweep active per
    app instance at a time) - revisit if that ever changes.
    """
    with _status_lock:
        status = load_project_status(project_dir, project_name, guv_path, settings_path, sim_type)
        combo = status["combos"].setdefault(_combo_key(z, ach), {})
        combo["z"] = z
        combo["ach"] = ach
        combo.update(fields)
        save_project_status(project_dir, project_name, status)
        return status


def get_ach_base_record(project_dir, project_name, ach):
    """The raw ach_bases[...] record for this ACH, regardless of whether
    its flow_fingerprint matches anything or its directories still exist
    on disk - see find_reusable_ach_base for the validated version. This
    is for detecting a genuine fingerprint MISMATCH (as opposed to "no
    record exists at all") - a mismatch means the flow-affecting settings
    changed since this ACH's shared base/control were last built, so any
    leftover scratch directories are known-stale and safe to discard
    (see scenario_runs.py's own stale-scratch-discard logic); "no record"
    just means this ACH/project predates this feature or hasn't been
    built yet, which needs no special handling at all.
    """
    status = load_project_status(project_dir, project_name)
    return status.get("ach_bases", {}).get(_ach_label(ach))


def update_ach_base_status(project_dir, project_name, ach, flow_fingerprint, base_dir, control_dir,
                            control_results, guv_path=None, settings_path=None, sim_type=None):
    """Record the shared per-ACH flow base + UV-off control run so a later
    sweep launch (see find_reusable_ach_base) can validate before reusing
    them - keyed by ACH (not by Z/ACH combo), since the base/control are
    shared across every Z at that ACH, not owned by any one combo (see
    scenario_runs.py's ACH-major build_ach_fn).

    guv_path/settings_path/sim_type: forwarded straight to
    load_project_status, same as update_combo_status's own params - matters
    because build_ach_fn runs BEFORE any combo's own update_combo_status
    call for a fresh project (ACH-major order), so without these, this
    would be the first write to a brand-new status file and would set
    every top-level field to None; per load_project_status/update_combo_
    status's own "never overwrite what's already there" contract
    (setdefault - only fills an ABSENT key, not a None one), a later
    update_combo_status call could then never fix that None back to the
    real value.
    """
    with _status_lock:
        status = load_project_status(project_dir, project_name, guv_path, settings_path, sim_type)
        ach_bases = status.setdefault("ach_bases", {})
        ach_bases[_ach_label(ach)] = {
            "ach": ach, "flow_fingerprint": flow_fingerprint,
            "base_dir": base_dir, "control_dir": control_dir,
            "control_results": control_results, "updated_at": now_iso(),
        }
        save_project_status(project_dir, project_name, status)
        return status


def clear_ach_bases(project_dir, project_name):
    """Drop every recorded ach_bases entry - the status-file counterpart to
    physically deleting the _base_ACH*/_control_ACH* scratch directories
    (see the "Clean up shared scratch directories" action): without this,
    a stale record would still claim those directories exist, and the
    next sweep launch's find_reusable_ach_base would need to fall through
    to its own is_dir() check to notice they're gone - harmless either
    way, but clearing the record here keeps the status file's own "what's
    actually reusable" story honest without relying on that fallback.
    """
    with _status_lock:
        status = load_project_status(project_dir, project_name)
        status["ach_bases"] = {}
        save_project_status(project_dir, project_name, status)
        return status


def find_reusable_ach_base(project_dir, project_name, ach, flow_fingerprint):
    """The recorded ach_bases[...] entry for this ACH if its own
    flow_fingerprint matches the one passed in AND both its base_dir/
    control_dir still exist on disk, else None.

    The on-disk existence check matters even when the fingerprint matches:
    a directory can be deleted independently of this status file (a user's
    own cleanup, or the "Clean up shared scratch directories" action) -
    the JSON record alone is never sufficient proof the directories are
    still there to reuse.
    """
    status = load_project_status(project_dir, project_name)
    record = status.get("ach_bases", {}).get(_ach_label(ach))
    if not record or record.get("flow_fingerprint") != flow_fingerprint:
        return None
    if not (Path(record["base_dir"]).is_dir() and Path(record["control_dir"]).is_dir()):
        return None
    return record
