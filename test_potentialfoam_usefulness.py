#!/usr/bin/env python3
"""Is potentialFoam's own cost worth the better starting guess it gives -
for SIMPLE and for LTS separately? LTS's whole design point is adaptive
per-cell time stepping, which might make it less dependent on a good
initial guess than SIMPLE's single global relaxation factor - this tests
that hypothesis directly rather than assuming it.

4 combinations, same mesh, same everything except potentialFoam on/off:
  simple_with_pf / simple_no_pf / lts_with_pf / lts_no_pf

Each builds a FRESH case (mesh + initial fields only, via setup_case with
converge_flow=False - the fluence/cellZone step it still runs afterward is
expected to raise on this 0-lamp room; that's caught and ignored, it's
irrelevant to this test), then times converge_flow_field() directly with
the desired skip_potential_flow value - this is the number that actually
answers the question, and it already includes potentialFoam's own runtime
cost in the "with_pf" cases, so "faster overall" directly means "net
beneficial", no separate accounting needed.
"""
import re
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from guv_calcs import Project

from guvcfd.run_pipeline import setup_case, converge_flow_field, FlowConvergenceUndecided
from guvcfd.wsl_utils import wsl_path, read_wsl_text

GUV_PATH = r"C:\Users\hukcl\Documents\OpenFoam\Sandberg_simulations\sandberg_test_room.guv"
TEST_CASE_ROOT = r"\\wsl.localhost\Ubuntu\home\hclaus\OpenFOAM\hclaus-v2412\run\potentialfoam_test"
TEMPLATE_CASE_DIR = str(Path(__file__).resolve().parent / "guvcfd" / "templates" / "case_template")

CELL_SIZE = 0.1  # 40,824 cells - same mesh as the method comparison, for a fair reference point
MAX_ITERATIONS = 4000  # generous - skip_potential_flow=True starts from a cruder guess, may need more
NOTES_PATH = Path(__file__).resolve().parent / "potentialfoam_test_NOTES.md"

COMBINATIONS = [
    ("simple_with_pf", dict(method="simple", skip_potential_flow=False)),
    ("simple_no_pf", dict(method="simple", skip_potential_flow=True)),
    ("lts_with_pf", dict(method="lts", skip_potential_flow=False)),
    ("lts_no_pf", dict(method="lts", skip_potential_flow=True)),
]


def _field_stats(case_dir_wsl, relative_path, is_vector):
    try:
        content = read_wsl_text(f"{case_dir_wsl}/0/{relative_path}")
    except RuntimeError:
        return None
    pattern = (r'internalField\s+nonuniform\s+List<vector>\s*\n(\d+)\s*\n\(\n(.*?)\n\)' if is_vector else
               r'internalField\s+nonuniform\s+List<scalar>\s*\n(\d+)\s*\n\(\n(.*?)\n\)')
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        return None
    if is_vector:
        vals = []
        for line in m.group(2).splitlines():
            line = line.strip().strip("()")
            if line:
                x, y, z = (float(v) for v in line.split())
                vals.append((x * x + y * y + z * z) ** 0.5)
    else:
        vals = [float(v) for v in m.group(2).splitlines() if v.strip()]
    if not vals:
        return None
    return {"mean": float(np.mean(vals)), "max": float(np.max(vals))}


project = Project.load(GUV_PATH)
room = next(iter(project.rooms.values()))

setup_note = f"""# potentialFoam usefulness test

Started: {datetime.now().isoformat(timespec='seconds')}

**Question**: does potentialFoam's cheap inviscid pre-solve actually save
net time, for SIMPLE and for LTS separately? Hypothesis: LTS's adaptive
per-cell time stepping may make it less dependent on a good starting
guess than SIMPLE, so potentialFoam might help SIMPLE more than LTS (or
even net-negative for LTS).

**Parameters**: room {room.x}x{room.y}x{room.z} {room.units}, cell size
{CELL_SIZE}m (40,824 cells), iteration cap {MAX_ITERATIONS}, rel_tol 1%
(defaults). Fresh case per combination (mesh + initial fields only, no
flow-field reuse across combinations) so potentialFoam on/off is the ONLY
thing that differs at the start.

**Combinations**: {', '.join(f'`{n}`' for n, _ in COMBINATIONS)}

**Case directories**: `{TEST_CASE_ROOT}/<name>/`

---

## Results
"""
NOTES_PATH.write_text(setup_note)
print(f"Wrote setup notes to {NOTES_PATH}")

results = []

for name, opts in COMBINATIONS:
    case_dir = f"{TEST_CASE_ROOT}/{name}"
    case_dir_wsl = wsl_path(case_dir)
    print(f"\n=== {name} (method={opts['method']}, skip_potential_flow={opts['skip_potential_flow']}) ===")

    print("  Building mesh + initial fields (no flow convergence yet)...")
    try:
        setup_case(
            GUV_PATH, case_dir, template_case_dir=TEMPLATE_CASE_DIR,
            Z=6.0, ach=2.0,
            inlet_wall="ceiling", inlet_center=(0.5, 0.5), inlet_size=(0.3, 0.3),
            inlet_diffuser_type="direct",
            outlet_wall="backWall", outlet_center=(0.5, 0.8518518518518519), outlet_size=(0.4, 0.4),
            cell_size=CELL_SIZE, nbins=5,
            converge_flow=False,
            log_fn=print,
        )
    except Exception as e:
        # _finish_case_setup's fluence/decay-rate check always fires on
        # this 0-lamp room regardless of flow state - irrelevant here,
        # mesh + initial fields are already on disk by the time it raises.
        print(f"  (expected post-setup error on this 0-lamp room, ignored: {e})")

    print(f"  Timing converge_flow_field (skip_potential_flow={opts['skip_potential_flow']})...")
    t0 = time.time()
    outcome, detail = None, None
    try:
        _, converged = converge_flow_field(
            case_dir, n_iterations=500, method=opts["method"], rel_tol=0.01,
            max_iterations=MAX_ITERATIONS, skip_potential_flow=opts["skip_potential_flow"],
            log_fn=print,
        )
        outcome = f"converged={converged}"
    except FlowConvergenceUndecided as e:
        outcome = f"hit the {MAX_ITERATIONS}-iteration cap without a verdict"
        detail = str(e)
    elapsed = time.time() - t0

    U_stats = _field_stats(case_dir_wsl, "U", is_vector=True)
    p_stats = _field_stats(case_dir_wsl, "p", is_vector=False)
    results.append({"name": name, "elapsed_s": elapsed, "outcome": outcome, "detail": detail,
                     "U_mag_stats": U_stats, "p_stats": p_stats})
    print(f"--- {name}: {elapsed:.1f}s, {outcome} ---")

print("\n=== SUMMARY ===")
summary_lines = []
for r in results:
    line = f"  {r['name']:20s}  {r['elapsed_s']:7.1f}s   {r['outcome']}"
    print(line)
    summary_lines.append(line)
    if r["U_mag_stats"]:
        u_line = f"                        U magnitude: mean={r['U_mag_stats']['mean']:.4g} max={r['U_mag_stats']['max']:.4g}"
        print(u_line)
        summary_lines.append(u_line)
    if r["p_stats"]:
        p_line = f"                        p: mean={r['p_stats']['mean']:.4g} max={r['p_stats']['max']:.4g}"
        print(p_line)
        summary_lines.append(p_line)

with open(NOTES_PATH, "a") as f:
    f.write(f"\nFinished: {datetime.now().isoformat(timespec='seconds')}\n\n```\n")
    f.write("\n".join(summary_lines))
    f.write("\n```\n\n**How to read this**: `elapsed_s` is converge_flow_field()'s own wall-clock "
            "time (mesh gen not included) - for the *_with_pf combos, this INCLUDES potentialFoam's "
            "own runtime, so a with_pf combo being faster overall already means potentialFoam was "
            "net beneficial for that method, no separate accounting needed. Compare simple_with_pf "
            "vs simple_no_pf, and lts_with_pf vs lts_no_pf, independently - the question is whether "
            "potentialFoam helps EACH method, not a cross-method comparison (that's what "
            "compare_convergence_methods.py already covers).\n")
print(f"\nAppended results to {NOTES_PATH}")
