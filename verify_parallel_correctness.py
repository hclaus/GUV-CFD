#!/usr/bin/env python3
"""Correctness check for the new MPI-parallel decomposition code:
does `parallel_simple` (from compare_convergence_methods.py, currently
sitting at 1500 non-converged iterations) reach the SAME converged answer
as `serial_simple` (already converged at 1000 iterations), if let run to
actual convergence with no artificial iteration cap?

Resumes parallel_simple's EXISTING case directory (reuses its 1500
iterations of progress via converge_flow_field's resume=True - does NOT
restart from scratch) with a generous iteration budget, then compares its
final U/p field against serial_simple's already-converged state.
"""
import re
import time

import numpy as np

from guvcfd.run_pipeline import converge_flow_field, FlowConvergenceUndecided
from guvcfd.wsl_utils import wsl_path, read_wsl_text

ROOT = wsl_path(r"\\wsl.localhost\Ubuntu\home\hclaus\OpenFOAM\hclaus-v2412\run\convergence_compare")
PARALLEL_CASE = f"{ROOT}/parallel_simple"
PARALLEL_CASE_WIN = r"\\wsl.localhost\Ubuntu\home\hclaus\OpenFOAM\hclaus-v2412\run\convergence_compare\parallel_simple"
SERIAL_CASE = f"{ROOT}/serial_simple"

GENEROUS_MAX_ITERATIONS = 8000  # 1500 already done + plenty of room to actually converge
N_PROCS = 4


def read_vector_stats(case_wsl, relative_path):
    content = read_wsl_text(f"{case_wsl}/{relative_path}")
    m = re.search(r'internalField\s+nonuniform\s+List<vector>\s*\n(\d+)\s*\n\(\n(.*?)\n\)', content, re.DOTALL)
    rows = []
    for line in m.group(2).splitlines():
        line = line.strip().strip("()")
        if line:
            rows.append([float(x) for x in line.split()])
    mag = np.linalg.norm(np.array(rows), axis=1)
    return {"mean": float(mag.mean()), "max": float(mag.max()), "min": float(mag.min())}


def read_scalar_stats(case_wsl, relative_path):
    content = read_wsl_text(f"{case_wsl}/{relative_path}")
    m = re.search(r'internalField\s+nonuniform\s+List<scalar>\s*\n(\d+)\s*\n\(\n(.*?)\n\)', content, re.DOTALL)
    vals = np.array([float(v) for v in m.group(2).splitlines() if v.strip()])
    return {"mean": float(vals.mean()), "max": float(vals.max()), "min": float(vals.min())}


print(f"Resuming parallel_simple from its existing 1500-iteration state, "
      f"budget extended to {GENEROUS_MAX_ITERATIONS} iterations, n_procs={N_PROCS}...")
t0 = time.time()
outcome = None
try:
    _, converged = converge_flow_field(
        PARALLEL_CASE_WIN, n_iterations=500, method="simple", rel_tol=0.01,
        max_iterations=GENEROUS_MAX_ITERATIONS, n_procs=N_PROCS, resume=True,
        log_fn=print,
    )
    outcome = f"converged={converged}"
except FlowConvergenceUndecided as e:
    outcome = f"still undecided after {GENEROUS_MAX_ITERATIONS} iterations: {e}"
elapsed = time.time() - t0
print(f"\n--- parallel_simple resume finished in {elapsed:.1f}s: {outcome} ---")

print("\n=== Final field comparison: serial_simple (converged @1000) vs parallel_simple (extended) ===")
for label, case_wsl in (("serial_simple", SERIAL_CASE), ("parallel_simple", PARALLEL_CASE)):
    U = read_vector_stats(case_wsl, "0/U")
    p = read_scalar_stats(case_wsl, "0/p")
    print(f"{label}: U mag mean={U['mean']:.6g} max={U['max']:.6g}   "
          f"p mean={p['mean']:.6g} max={p['max']:.6g}")
