# Does solver choice (SIMPLE vs PIMPLE) explain the decay-vs-steady-state eACH_uv gap?

**Short answer: no.** PIMPLE and SIMPLE agree with each other to within ~12%,
both landing 2.2-2.5x away from decay mode's answer for the same case. The
solver-choice hypothesis is now ruled out; the gap lives somewhere else.

## Background

GUV-CFD has two independent ways to estimate `eACH_uv` (equivalent UV
air-changes) for a room: **decay mode** (transient, room starts uniformly
contaminated, watch it clear under `pimpleFoam`) and **steady-state mode**
(continuous point-source injection under `simpleFoam`, Phase 1
ventilation-only then Phase 2 +UV, read off equilibrium concentrations).
On the patient ward room, these two methods have disagreed by a consistent
~1.8-2.8x across every Z/ACH combo tried (steady always higher) - see
`v10_v11_decay_vs_steadystate_comparison.csv`. Four earlier hypotheses were
investigated and rejected with real CFD data (UV/concentration spatial
correlation, fluence-binning resolution, source size/concentration, Phase 2
under-convergence), and a real bug was found and fixed along the way
(steady-state's ventilation baseline was Phase-1-derived and mixing-lag
biased; switched to a dedicated UV-off control run matching decay's own
method - didn't close the gap either).

The leading remaining hypothesis was a genuine mathematical/numerical
difference between the two methods, and solver choice was the most obvious
candidate: **steady-state's Phase 1/2 both run under `simpleFoam` (SIMPLE)**,
which has no `ddt()` term at all for U/p/k/omega - it's pure pseudo-time
relaxation, not real physical time. Once the flow field is accepted -
including via the "bounded oscillation" acceptance path
`converge_flow_field()` uses for cases (like Z=6/ACH=6, tested here) that
never reach a strict residual-based verdict - **it stays frozen at whatever
phase of its oscillation it happened to be accepted at**, and only T is
marched forward against that one frozen snapshot. Decay mode, by contrast,
runs a genuinely transient `pimpleFoam` solve where U/p/k/omega keep
evolving (and oscillating) in real time throughout.

This test asks directly: if steady-state's Phase 2 were run under a real
transient solver instead - flow field genuinely evolving, not frozen -
would it land on the same `T_ss`, or would it land closer to decay mode's
answer?

## Method

Reused the already-converged, already-run `Z6_ACH6` case directory from the
`patient_ward_4B1_v11_simsstate` project (steady-state mode, the same combo
`v10_v11_decay_vs_steadystate_comparison.csv` flags at ratio 2.25). Its
`constant/fvOptions` already held Phase 2's exact configuration (contaminant
source + all 25 UV zones active, frozen there since Phase 2 was the last
thing that ran).

1. Cloned the case directory (mesh, BCs, converged flow field, Phase 2's
   own fvOptions all reused as-is).
2. Reset T to a cold start (uniform 0 - same convention Phase 1 itself
   already uses). U/p/k/omega/nut internal fields left untouched.
3. Ran `pimpleFoam` directly (not `simpleFoam`) against the same mesh, in
   300s chunks (`startFrom latestTime` between chunks), letting U/p/k/omega
   evolve freely rather than staying frozen.
4. Tracked the live room-average `volAverage(T)` per timestep (same
   `volAverageLive1` function object the rest of the pipeline already
   uses), and checked for a plateau via the same windowed-CV method
   (`decay_analysis.check_plateau_windowed`, trailing 15% window, 1% CV
   threshold) `_run_phase` uses for Phase 1/2.
5. Derived `eACH_uv` from the resulting `T_ss` via the same
   `compute_corrected_eACH_uv_from_control` formula steady-state already
   uses, against the same measured ventilation rate
   (`ventilation_ach_measured = 3.749/hr`, this combo's own control-run
   result already on disk).

Script: `validate_pimple_vs_simple_steady_state.py` (repo root, one-off,
not wired into the GUI).

One real bug found and fixed along the way: `set_control_dict_time()`'s
blanket `writeInterval` rewrite (needed so `scalarTransport1`'s own nested
`writeInterval` tracks the main solve) also clobbered `volAverageLive1`'s
writeInterval down to one sample per chunk - fixed by re-pinning it to 1
(every timestep) after every `set_control_dict_time()` call, the same
`set_function_write_interval` idiom `_run_phase` already uses. Also
benefited directly from the same-day `maxCo` fix (0.5 -> 5): this run
plateaued in ~900s of simulated time inside ~10 minutes of wall-clock,
across 3 chunks, with zero stalls.

## Results

| Quantity | SIMPLE (existing Phase 2) | PIMPLE (this test) |
|---|---|---|
| T_ss (windowed) | 0.028268 | 0.024892 (CV 0.74%, plateaued) |
| T_ss (T-infinity extrapolated) | 0.027631 | - |
| eACH_uv_corrected [/hr] | 61.39 | 68.56 |

For reference, decay mode's own answer for this exact combo (Z=6/ACH=6):
**eACH_uv_corrected = 27.26 /hr**.

- PIMPLE vs SIMPLE: `T_ss` ratio = 0.88 (PIMPLE ~12% lower), `eACH_uv`
  ratio = 1.12 (PIMPLE ~12% higher).
- PIMPLE vs decay: ratio = 2.52 (slightly **worse** than SIMPLE's 2.25 vs
  decay).

## Interpretation

Letting the flow field genuinely evolve (real transient PIMPLE) instead of
staying frozen at one oscillation snapshot (SIMPLE) moves the answer by
~12% - a real, measurable effect, but **in the wrong direction** to explain
the decay-vs-steady gap, and nowhere near its size (2.2-2.8x). Both
solvers, run on the identical mesh/BCs/source/UV configuration, land within
~12% of each other and both land 2.2-2.5x away from decay mode.

**This rules out solver choice / frozen-flow-field bias as the explanation
for the eACH_uv gap.** The two steady-state solve methods (pseudo-time
SIMPLE vs real-time PIMPLE) are mutually consistent; the actual discrepancy
must be somewhere in how the two *methods* (continuous-injection ratio vs
transient-decay curve fit) relate to each other mathematically, or in
something not yet tested. This directly narrows the search toward the
already-flagged leading hypothesis: a genuine mathematical difference
between decay's transient curve-fit and steady-state's mass-balance ratio -
worth checking next whether this room's decay curves show non-single-
exponential (multi-mode) behavior, which would bias a single-exponential
fit's rate constant in a way a steady-state equilibrium ratio wouldn't be
subject to at all.

## Artifacts

- `validate_pimple_vs_simple_steady_state.py` - the script (rerunnable
  against any other case directory by editing the constants at the top).
- `pimple_validation_log.txt` - full run log.
- `pimple_validation_result.json` - the result dict above, machine-readable.
- `pimple_validation_progress.json` - the full per-timestep t/T series
  (for plotting the transient buildup curve if useful later).
- WSL case directory (not committed, left on disk for inspection):
  `patient_ward_4B1_v11_simsstate/_pimple_validation_Z6_ACH6`.
