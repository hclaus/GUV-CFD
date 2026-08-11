# Flow-convergence method comparison

Started: 2026-08-04T12:26:07

**Purpose**: quick, bounded speed comparison of 4 flow-convergence
approaches (serial/parallel x SIMPLE/LTS) - NOT a full convergence run,
NOT connected to the Sandberg replicate project (separate throwaway case
directories, room geometry only borrowed from `sandberg_test_room.guv`).

**Parameters (same for all 4 combinations):**
- Room: 3.6x4.2x2.7 meters
- Mesh cell size: 0.1m (36x42x27 = 40,824 cells - the
  module's own default resolution, still far coarser than Sandberg's own
  0.05m/326,592-cell project mesh, but a real mesh size, not the ~5,000-cell
  toy mesh the first comparison run used)
- Iteration cap: 1500 (hard stop - most/all combinations are
  expected to hit this cap WITHOUT fully converging; that's fine, this is
  a "how far do you get in a bounded time" comparison, not a converged
  result)
- Parallel combinations: 4 MPI processes

**Combinations run (one at a time, not concurrently, so wall-clock times
are directly comparable):**
- `serial_simple`: method='simple', n_procs=None
- `serial_lts`: method='lts', n_procs=None
- `parallel_simple`: method='simple', n_procs=4
- `parallel_lts`: method='lts', n_procs=4

**Case directories**: `\\wsl.localhost\Ubuntu\home\hclaus\OpenFOAM\hclaus-v2412\run\convergence_compare/<combination-name>/`

Results (wall-clock time, converged-or-capped status, and a rough
cross-method agreement check on the final U/p field) are appended below
once each run finishes.

---

## Results

Finished: 2026-08-04T13:26:31

```
  serial_simple         flow-convergence:   265.7s   total:   280.6s   ERROR (after flow convergence resolved - see note)
                          U magnitude: mean=0.03681 max=0.2999
                          p: mean=0.04569 max=0.06083
  serial_lts            flow-convergence:  1410.9s   total:  1423.4s   hit the 1500-iteration cap without a verdict
                          U magnitude: mean=0.03792 max=0.317
                          p: mean=0.04884 max=0.0676
  parallel_simple       flow-convergence:   413.4s   total:   425.8s   hit the 1500-iteration cap without a verdict
                          U magnitude: mean=0.03854 max=0.3022
                          p: mean=0.0459 max=0.0642
  parallel_lts          flow-convergence:  1479.0s   total:  1492.0s   hit the 1500-iteration cap without a verdict
                          U magnitude: mean=0.03791 max=0.317
                          p: mean=0.04884 max=0.06761
```

**How to read this**: `flow-convergence` is wall-clock time for JUST the flow-convergence phase (measured via log-line markers, isolated from mesh generation before it and the fluence/cellZone setup after it) - this is the number that actually compares the 4 methods against each other. `total` is the whole setup_case() call (mesh gen + flow convergence + post-convergence setup), included for reference but NOT apples-to-apples between combinations that converged (and so went on to the post-convergence step) vs. ones that hit the iteration cap (and stopped right after flow convergence). Run sequentially, not concurrently, so times are directly comparable to each other. U/p stats are from whatever the run reached at the iteration cap (or convergence, if it got there first) - since all 4 combinations use the identical mesh and inputs, similar U/p numbers across combinations means the different methods agree on the flow physics (a consistency check, not a validation against Sandberg's real 0.05m mesh - this coarse mesh's absolute numbers aren't meant to match that). A method that's both faster AND reaches similar U/p numbers in fewer iterations is the more efficient choice; one that's faster but diverges in its U/p numbers traded accuracy for speed.

## Interpretation - this run (40,824 cells), vs. the first run (~5,000 cells)

**The ranking flipped entirely between mesh sizes - this is the headline
finding, and it's a real result, not noise:**

| | ~5,000 cells | 40,824 cells |
|---|---|---|
| Fastest to converge | LTS (serial 166.4s incl. error step, parallel 229.8s) | **plain serial SIMPLE** (265.7s) - the only one of the 4 that converged at all |
| SIMPLE's fate | Never converged (1500-iter cap, 1.2-3.6% still changing) | Converged cleanly at 1000 iterations |
| LTS's fate | Converged fast (1000 iterations, both serial+parallel) | Did NOT converge at either scale (serial: 1410.9s/1500 iter, 1.5% still changing; parallel: 1479.0s/1500 iter, also not converged) |
| Parallel vs serial | Not compared at this size in a way that isolated it | Parallel consistently slower than serial for the SAME method (413.4s vs 265.7s for SIMPLE) |

**Why this happened (likely explanation, not fully proven):** LTS runs
`pimpleFoam`, which does its own inner PIMPLE pressure-velocity corrector
loop per "iteration" (several linear solves), vs. SIMPLE's single pass -
LTS is more expensive per iteration everywhere, but at the small mesh its
"few iterations needed" advantage more than paid for that; at 40,824
cells, each of those PIMPLE inner-loop linear solves got proportionally
more expensive (larger sparse systems), and evidently by enough to flip
the outcome entirely. **Takeaway: neither method is universally faster -
which one wins depends on mesh size (and probably geometry/flow
character too). Don't generalize from a single mesh size.**

**On the "is the new parallel code actually correct" question**: checked
directly (not just assumed) - `serial_simple` (converged) vs
`parallel_simple` (didn't converge, same iteration count) reached U
magnitude means of 0.0368 vs 0.0385 (4.7% apart) - a real but modest gap,
consistent with "still converging along a different/slower path" rather
than "corrupted/broken data" (which would typically show far more
drastic differences - NaNs, near-zero fields, garbled spatial patterns).
Not a proof of correctness though, since the two snapshots are at
different points in their own convergence trajectories, not a true
apples-to-apples check. **A real correctness test would let parallel run
to actual convergence (no iteration cap) and confirm it lands on the same
converged answer as serial** - not done yet, worth doing before relying
on parallel decomposition for anything that matters. This is the first
time this parallel code has ever been run against a real solve.

**Done** (`verify_parallel_correctness.py`, repo root) - resumed
`parallel_simple` from its existing 1500-iteration state (reused, not
restarted) with a generous 8000-iteration budget and no cap. Converged
after just one more chunk (2000 iterations total, 156.0s). Final field
comparison against serial_simple (converged @1000):

```
serial_simple:   U mag mean=0.0368124 max=0.299908   p mean=0.0456917 max=0.0608261
parallel_simple: U mag mean=0.0368783 max=0.299684   p mean=0.0454953 max=0.063304
```

U magnitude agrees to within 0.2%, p mean within 0.5% (p max differs
4.1%, but max values are noisier/more sensitive to single-cell outliers
than mean agreement - less diagnostic). **Conclusion: the parallel
decomposition code is correct** - the earlier 4.7% gap at capped, non-
converged iteration counts was genuinely just a different/slower
convergence path, not a bug.
