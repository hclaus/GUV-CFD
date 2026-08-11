#!/usr/bin/env python3
"""Quick, BOUNDED comparison of flow-convergence approaches: serial vs.
parallel (MPI decomposition), SIMPLE vs. LTS (local time stepping) - 4
combinations, run one at a time (not concurrently, so wall-clock numbers
are comparable and not skewed by CPU contention between them).

Deliberately NOT run to full convergence - MAX_ITERATIONS is a hard,
modest cap so this finishes in a "decent time" for comparison purposes,
not a result you'd actually use. FlowConvergenceUndecided (raised when the
cap is hit without a verdict) is an EXPECTED outcome here, not a failure -
caught and reported like any other result.

Fully independent of the Sandberg project: builds fresh, throwaway case
directories under TEST_CASE_ROOT (uses Sandberg's .guv file only for room
geometry, never touches sandberg_test_room's own case directories).

Usage: uv run python compare_convergence_methods.py
Writes NOTES.md (human-readable) and compare_convergence_methods_results.json
(machine-readable) into TEST_CASE_ROOT, documenting exactly what was run,
with what parameters, and what came out - both written BEFORE the runs
start (so the setup is on record even if interrupted) and updated after.
"""
import json
import time
from datetime import datetime
from pathlib import Path

from guv_calcs import Project

from guvcfd.run_pipeline import setup_case, FlowConvergenceUndecided
from guvcfd.wsl_utils import wsl_path, read_wsl_text
import re

GUV_PATH = r"C:\Users\hukcl\Documents\OpenFoam\Sandberg_simulations\sandberg_test_room.guv"
TEST_CASE_ROOT = r"\\wsl.localhost\Ubuntu\home\hclaus\OpenFOAM\hclaus-v2412\run\convergence_compare"
TEMPLATE_CASE_DIR = str(Path(__file__).resolve().parent / "guvcfd" / "templates" / "case_template")

COARSE_CELL_SIZE = 0.1  # 36x42x27 = 40,824 cells - the module's own default resolution (still far
                         # coarser than Sandberg's own 0.05m/326,592-cell project mesh, but no longer
                         # the artificially tiny ~5,000-cell mesh the first comparison used)
MAX_ITERATIONS = 1500   # hard cap - a bounded "how far do we get" comparison, not full convergence
N_PROCS = 4              # per combination - leaves headroom on a 12-core machine

# Log-line markers used to time the flow-convergence step in isolation,
# separate from mesh generation (before) and the post-convergence fluence/
# cellZone step (after) - see _TimedLogger. "Converging flow field" is
# setup_case's own first line of that phase; "Computing fluence rate" is
# _finish_case_setup's first line, reached the instant flow convergence
# resolves one way or another (converged, accepted, or - for the
# FlowConvergenceUndecided path - never reached at all, in which case the
# except block below closes out the timer itself).
_FLOW_START_MARKER = "Converging flow field"
_FLOW_END_MARKER = "Computing fluence rate"


class _TimedLogger:
    """Wraps print() with log_fn's usual job, plus recording wall-clock
    timestamps at the flow-convergence phase's start/end - gives a clean,
    convergence-only duration regardless of what happens before (mesh
    generation) or after (fluence/cellZone setup, including the unrelated
    "0 lamps" error the first comparison run hit)."""

    def __init__(self):
        self.t_start = None
        self.t_end = None

    def __call__(self, line):
        print(line)
        if _FLOW_START_MARKER in line and self.t_start is None:
            self.t_start = time.time()
        elif _FLOW_END_MARKER in line and self.t_end is None:
            self.t_end = time.time()

    def close_if_open(self):
        """Call after setup_case() returns/raises - covers the
        FlowConvergenceUndecided path, where _FLOW_END_MARKER is never
        logged because _finish_case_setup is never reached."""
        if self.t_start is not None and self.t_end is None:
            self.t_end = time.time()

    @property
    def flow_convergence_s(self):
        if self.t_start is None or self.t_end is None:
            return None
        return self.t_end - self.t_start

COMBINATIONS = [
    ("serial_simple", dict(method="simple", n_procs=None)),
    ("serial_lts", dict(method="lts", n_procs=None)),
    ("parallel_simple", dict(method="simple", n_procs=N_PROCS)),
    ("parallel_lts", dict(method="lts", n_procs=N_PROCS)),
]

NOTES_PATH = Path(__file__).resolve().parent / "convergence_compare_NOTES.md"


def _field_stats(case_dir_wsl, relative_path, is_vector):
    """Best-effort mean/max of a field's internalField, for a rough
    cross-method agreement check - None if the field can't be read (e.g.
    the run failed before writing anything to 0/)."""
    try:
        content = read_wsl_text(f"{case_dir_wsl}/0/{relative_path}")
    except RuntimeError:
        return None
    if is_vector:
        m = re.search(r'internalField\s+nonuniform\s+List<vector>\s*\n(\d+)\s*\n\(\n(.*?)\n\)', content, re.DOTALL)
        if not m:
            return None
        vals = []
        for line in m.group(2).splitlines():
            line = line.strip().strip("()")
            if line:
                x, y, z = (float(v) for v in line.split())
                vals.append((x * x + y * y + z * z) ** 0.5)
    else:
        m = re.search(r'internalField\s+nonuniform\s+List<scalar>\s*\n(\d+)\s*\n\(\n(.*?)\n\)', content, re.DOTALL)
        if not m:
            return None
        vals = [float(v) for v in m.group(2).splitlines() if v.strip()]
    if not vals:
        return None
    return {"mean": sum(vals) / len(vals), "max": max(vals), "min": min(vals)}


project = Project.load(GUV_PATH)
room = next(iter(project.rooms.values()))

setup_note = f"""# Flow-convergence method comparison

Started: {datetime.now().isoformat(timespec='seconds')}

**Purpose**: quick, bounded speed comparison of 4 flow-convergence
approaches (serial/parallel x SIMPLE/LTS) - NOT a full convergence run,
NOT connected to the Sandberg replicate project (separate throwaway case
directories, room geometry only borrowed from `sandberg_test_room.guv`).

**Parameters (same for all 4 combinations):**
- Room: {room.x}x{room.y}x{room.z} {room.units}
- Mesh cell size: {COARSE_CELL_SIZE}m (36x42x27 = 40,824 cells - the
  module's own default resolution, still far coarser than Sandberg's own
  0.05m/326,592-cell project mesh, but a real mesh size, not the ~5,000-cell
  toy mesh the first comparison run used)
- Iteration cap: {MAX_ITERATIONS} (hard stop - most/all combinations are
  expected to hit this cap WITHOUT fully converging; that's fine, this is
  a "how far do you get in a bounded time" comparison, not a converged
  result)
- Parallel combinations: {N_PROCS} MPI processes

**Combinations run (one at a time, not concurrently, so wall-clock times
are directly comparable):**
{chr(10).join(f'- `{name}`: method={opts["method"]!r}, n_procs={opts["n_procs"]}'
              for name, opts in COMBINATIONS)}

**Case directories**: `{TEST_CASE_ROOT}/<combination-name>/`

Results (wall-clock time, converged-or-capped status, and a rough
cross-method agreement check on the final U/p field) are appended below
once each run finishes.

---

## Results
"""
NOTES_PATH.write_text(setup_note)
print(f"Wrote setup notes to {NOTES_PATH}")
print(f"Room: {room.x}x{room.y}x{room.z} {room.units}, coarse cell size={COARSE_CELL_SIZE}m, "
      f"cap={MAX_ITERATIONS} iterations")

results = []

for name, opts in COMBINATIONS:
    case_dir = f"{TEST_CASE_ROOT}/{name}"
    case_dir_wsl = wsl_path(case_dir)
    print(f"\n=== {name} (method={opts['method']}, n_procs={opts['n_procs']}) ===")
    t0 = time.time()
    logger = _TimedLogger()
    outcome, detail = None, None
    try:
        summary = setup_case(
            GUV_PATH, case_dir, template_case_dir=TEMPLATE_CASE_DIR,
            Z=6.0, ach=2.0,
            inlet_wall="ceiling", inlet_center=(0.5, 0.5), inlet_size=(0.3, 0.3),
            inlet_diffuser_type="direct",
            outlet_wall="backWall", outlet_center=(0.5, 0.8518518518518519), outlet_size=(0.4, 0.4),
            cell_size=COARSE_CELL_SIZE, nbins=5,
            flow_convergence_method=opts["method"], flow_max_iterations=MAX_ITERATIONS,
            n_procs=opts["n_procs"],
            log_fn=logger,
        )
        outcome = "converged" if summary.get("flow_converged") else "accepted (bounded oscillation)"
    except FlowConvergenceUndecided as e:
        outcome = f"hit the {MAX_ITERATIONS}-iteration cap without a verdict"
        detail = str(e)
    except Exception as e:
        outcome = "ERROR (after flow convergence resolved - see note)"
        detail = str(e)
    finally:
        logger.close_if_open()

    elapsed = time.time() - t0
    U_stats = _field_stats(case_dir_wsl, "U", is_vector=True)
    p_stats = _field_stats(case_dir_wsl, "p", is_vector=False)
    results.append({"name": name, "elapsed_s": elapsed, "flow_convergence_s": logger.flow_convergence_s,
                     "outcome": outcome, "detail": detail,
                     "U_mag_stats": U_stats, "p_stats": p_stats})
    fc = f"{logger.flow_convergence_s:.1f}s" if logger.flow_convergence_s is not None else "n/a"
    print(f"--- {name}: total={elapsed:.1f}s, flow-convergence-only={fc}, {outcome} ---")

print("\n=== SUMMARY ===")
summary_lines = []
for r in results:
    fc = f"{r['flow_convergence_s']:7.1f}s" if r["flow_convergence_s"] is not None else "    n/a"
    line = f"  {r['name']:20s}  flow-convergence: {fc}   total: {r['elapsed_s']:7.1f}s   {r['outcome']}"
    print(line)
    summary_lines.append(line)
    if r["U_mag_stats"]:
        u_line = (f"                          U magnitude: mean={r['U_mag_stats']['mean']:.4g} "
                  f"max={r['U_mag_stats']['max']:.4g}")
        print(u_line)
        summary_lines.append(u_line)
    if r["p_stats"]:
        p_line = (f"                          p: mean={r['p_stats']['mean']:.4g} "
                  f"max={r['p_stats']['max']:.4g}")
        print(p_line)
        summary_lines.append(p_line)

with open("compare_convergence_methods_results.json", "w") as f:
    json.dump(results, f, indent=2)

with open(NOTES_PATH, "a") as f:
    f.write(f"\nFinished: {datetime.now().isoformat(timespec='seconds')}\n\n```\n")
    f.write("\n".join(summary_lines))
    f.write("\n```\n\n**How to read this**: `flow-convergence` is wall-clock time for JUST the "
            "flow-convergence phase (measured via log-line markers, isolated from mesh generation "
            "before it and the fluence/cellZone setup after it) - this is the number that actually "
            "compares the 4 methods against each other. `total` is the whole setup_case() call "
            "(mesh gen + flow convergence + post-convergence setup), included for reference but NOT "
            "apples-to-apples between combinations that converged (and so went on to the "
            "post-convergence step) vs. ones that hit the iteration cap (and stopped right after "
            "flow convergence). Run sequentially, not concurrently, so times are directly comparable "
            "to each other. U/p stats are from whatever the run reached at the iteration cap (or "
            "convergence, if it got there first) - since all 4 combinations use the identical mesh "
            "and inputs, similar U/p numbers across combinations means the different methods agree "
            "on the flow physics (a consistency check, not a validation against Sandberg's real "
            "0.05m mesh - this coarse mesh's absolute numbers aren't meant to match that). A method "
            "that's both faster AND reaches similar U/p numbers in fewer iterations is the more "
            "efficient choice; one that's faster but diverges in its U/p numbers traded accuracy "
            "for speed.\n")
print(f"\nAppended results to {NOTES_PATH}")
