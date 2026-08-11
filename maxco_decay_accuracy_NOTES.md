# maxCo effect on decay-mode accuracy

Started: 2026-08-07T08:53:27

**Purpose**: isolate the effect of pimpleFoam's `maxCo` (adaptive-timestep
Courant cap) on the FITTED ventilation decay rate (-> measured ACH), not
just on numerical stability (already confirmed separately on the live
production sweep). Uses the real 4x5x3 project's own room/inlet/outlet/
mesh/ACH=3 settings, mechanical-ACH-only (no UV), so this speaks directly
to that project's own results.

**Fixed across every maxCo value** (only maxCo itself varies):
- Room: 4.0x5.0x3.0 meters, mesh cell size 0.1m
- Inlet: xMin wall, (2.5, 0.4)m, 0.2x0.2m, ceiling diffuser
- Outlet: xMax wall, (2.5, 2.7)m, 0.2x0.2m
- Nominal ACH: 3.0 /hr
- Decay run duration: 2763.0s (90% decay target, same rule
  production uses - see scenario_runs._decay_run_durations)
- pimple_delta_t (initial): 0.5s, nOuterCorrectors=3 (template default, unchanged)
- momentum/scalar relaxation: 0.7/0.7, scalarTransport nCorr=3 tol=0.0001

**maxCo values tested**: [5, 10, 15] - run CONCURRENTLY (separate
processes/cores) once this base is built, since this test compares
accuracy, not speed, so cross-run CPU contention doesn't invalidate it.

**Method**: ONE flow-converged base case is built once (mechanical-ACH-only,
so no UV/fluence pipeline involved), then cloned into one independent
ventilation-only control run per maxCo value
(ventilation_control.prepare_ventilation_only_control) - flow-convergence
cost is paid once and shared, so differences between runs below are due
to maxCo alone, not flow-convergence noise.

**Case directories**: `\\wsl.localhost\Ubuntu\home\hclaus\OpenFOAM\hclaus-v2412\run\maxco_decay_compare/base` (shared flow field),
`\\wsl.localhost\Ubuntu\home\hclaus\OpenFOAM\hclaus-v2412\run\maxco_decay_compare/control_maxco_<N>` (one per maxCo value).

Results (measured ACH from the fitted decay curve, fit uncertainty, and
wall-clock time) are appended below once every leg finishes.

---

## Results

Finished: 2026-08-07T13:13:51

```
Base case build: 202.2s
  maxCo=5    measured ACH=   2.667/hr  vs maxCo=5: (baseline)   wall-clock=15379.5s
  maxCo=10   measured ACH=     2.6/hr  vs maxCo=5:     -2.51%   wall-clock= 9781.5s
  maxCo=15   measured ACH=   2.533/hr  vs maxCo=5:     -5.03%   wall-clock= 6960.7s
```

**How to read this**: `measured ACH` is the actual ventilation air-change rate the CFD run's own decay curve implies (fit via decay_analysis.fit_effective_decay_rate), not the nominal 3.0/hr input - the two SHOULD agree closely if maxCo isn't biasing the result. The `%` column compares each maxCo against the maxCo=5 baseline (the conservative default): if it stays small (well inside noise/fit uncertainty) up through maxCo=10, that's real evidence today's production setting isn't trading away accuracy for speed. A clear trend that grows with maxCo (especially by maxCo=15) confirms there IS a real accuracy cost, and pins down roughly where it starts to bite.
