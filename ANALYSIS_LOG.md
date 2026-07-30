# Analysis Log

A running record of findings from GUV-CFD simulation results — as opposed to
`CHANGELOG.md`, which records changes to the tool itself. Each entry documents
what a run's data showed, the statistics behind it, and the physical
interpretation, so results don't have to be re-derived from scratch in a later
session.

---

## 2026-07-25 — `patient_ward_4B1_v8_simdcay`: effective eACH_UV scales sub-linearly with Z, not proportionally

**Run**: full Z × ACH sweep (Scenario Runs), Z ∈ {1.7, 3.4, 6, 8.5} cm²/mJ ×
nominal ACH ∈ {3, 6, 9}/hr, 12 combinations, `patient_ward_4B1_v8_simdcay`
under `~/OpenFOAM/hclaus-v2412/run/`. First real use of the batch sweep
feature added 2026-07-19.

**Question**: the well-mixed model assumes `eACH_uv_well_mixed = Z × mean
fluence rate × 3.6` — a direct multiplication, so it is *exactly* proportional
to Z by construction (verified: `eACH_well_mixed / Z` = 10.330 across all 12
runs, std ≈ 3×10⁻¹⁵). Does the CFD-fit *effective* eACH (the real,
imperfectly-mixed room's actual decay rate, minus a ventilation baseline) show
that same proportionality, or something else?

**Data fields used**: `eACH_uv_effective_corrected` (CFD decay-curve fit,
baselined on the *measured* ventilation-only decay rate from a UV-off control
run, not the nominal ACH setpoint — see `decay_analysis.compute_effective_eACH`
and `write_results_summary`'s `_corrected` fields) and
`mixing_efficiency_corrected` (= effective ÷ well-mixed).

### Finding 1 — Sub-linear power law, not proportional

Per-ACH log-log fits of `eACH_effective = a·Z^b`:

| nominal ACH | b (exponent) | 95% CI | R² | p (b = 1) |
|---|---|---|---|---|
| 3 | 0.799 | [0.705, 0.893] | 0.9985 | 0.012 |
| 6 | 0.904 | [0.856, 0.952] | 0.9997 | 0.013 |
| 9 | 0.929 | [0.893, 0.966] | 0.9998 | 0.014 |

All three reject b = 1 (proportional) with p < 0.02. Doubling Z delivers
roughly a 1.7–1.9× gain in effective eACH, not 2×.

**Is this fit noise?** No — each run's own CFD decay-curve regression is very
tight (fit_n = 101–242 points, 95% CI half-width only 0.13–0.20% of the eACH
value), while the deviation from a proportional prediction reaches 10.9–28.0%
depending on ACH — 50 to 200× larger than the statistical noise. This is a
physical saturation effect in the CFD transport, not a fitting artifact.

**Mechanism (hypothesis)**: mixing-limited (transport-limited) saturation.
Raising Z increases the local UV reaction rate near the lamps, but the rate at
which fresh (unirradiated) air is carried into that high-fluence zone — and
treated air carried back out to the rest of the room — is set by advection,
not by Z. Past a point, extra lamp power increasingly acts on air that has
already been treated on a prior pass through the high-fluence region, so the
marginal return on Z shrinks.

### Finding 2 — Ventilation moderates the saturation (Z × ACH interaction)

b rises toward 1 as nominal ACH increases (0.80 → 0.90 → 0.93): more airflow
measurably relieves the bottleneck, though it hasn't closed it even at ACH9.
A combined log-log regression across all 12 runs,
`log(eACH) = c₀ + b_Z·logZ + b_A·logACH + b_int·logZ·logACH`, found the
interaction term significant: **b_int = 0.122 ± 0.024, p = 0.0009**
(F-test for adding the interaction to a no-interaction model: F = 26.6,
p = 0.0009). A single Z-only exponent cannot describe this dataset — how much
is lost to imperfect mixing at a given Z depends on how much ventilation is
present.

Mixing efficiency (effective ÷ well-mixed) falls monotonically with Z in
every ACH group (Pearson r ≤ −0.99, p ≤ 0.007 per group), and the fall is
steepest at the lowest ventilation rate — worst case is the low-ACH/high-Z
corner (mixing efficiency 0.43 at ACH3/Z8.5) vs. best case low-Z/high-ACH
(0.64 at ACH9/Z1.7).

### Finding 3 — Consistency check: b and mixing-efficiency decline are the same signal

Since `eACH_well_mixed = 10.330·Z` exactly, `mixing_efficiency =
eACH_effective / (10.330·Z)`. If `eACH_effective = a·Z^b`, this forces
`mixing_efficiency = (a/10.330)·Z^(b−1)` — algebraically the same
nonlinearity, not independent evidence. Verified numerically: regressing
`log(mixing_efficiency)` on `log(Z)` directly reproduces `b − 1` to machine
precision in all three ACH groups (e.g. ACH3: −0.2010 both ways).

What *is* a useful check: does the eACH power-law fit (done in log-eACH
space) hold up when read back out in plain mixing-efficiency space? Yes — a
parity plot of predicted vs. actual mixing efficiency across all 12 runs
gives R² = 0.987, RMSE 0.4–1.0 percentage points, max error 1.2 points (at
ACH3, the noisiest group). The single number `1 − b` ("saturation strength")
alone predicts the *total* relative mixing-efficiency drop from Z=1.7→8.5
almost exactly:

| ACH | 1 − b | predicted drop | actual drop |
|---|---|---|---|
| 3 | 0.201 | 27.6% | 28.0% |
| 6 | 0.096 | 14.3% | 14.6% |
| 9 | 0.071 | 10.8% | 10.9% |

b also tracks the mean mixing-efficiency level across the 3 ACH groups
(r = 0.998) — but that's only 3 points (one per group), descriptive only,
not a real inference.

### Finding 4 (secondary) — Ventilation boundary condition under-delivers by a constant ~56%

Across all 12 runs, measured/nominal ventilation-flow ratio (`ach_delivery.ratio`
in each run's report) is essentially one constant: 0.4384 (ACH3), 0.4395
(ACH6), 0.4390 (ACH9) — mean 0.4390, std 0.0005, correlation with nominal ACH
r = 0.51 (p = 0.09, not significant). The inlet BC is delivering only ~44% of
its nominal flow rate at every setpoint tested — this looks like a fixed
geometric/short-circuiting loss (inlet sizing or duct routing in this
particular room mesh) rather than a flow-regime-dependent artifact. It's why
the `_corrected` eACH fields (baselined on measured ventilation, not nominal)
are the ones to trust for this room — they run 10–30% higher than the
uncorrected `eACH_uv_effective` and tell a materially different story.

**Caveat**: this ~44% ratio is specific to this room/mesh's inlet geometry —
don't assume it generalizes. If a future room's sweep shows a very different
delivery ratio, that's expected; if a *nominally identical* room/mesh setup
shows a different ratio, treat that as a regression to investigate.

**Full write-up with interactive graphs**: published as a Claude artifact,
"eACH vs Z — patient_ward_4B1_v8_simdcay" (private; ask if you need the link
re-shared — artifact URLs aren't stable across machines/sessions unless
explicitly reshared).

**Open questions for a future sweep**: does the same sub-linear b(ACH)
relationship and ~constant delivery ratio hold on a different room geometry,
or is the specific exponent range (0.80–0.93) and the 44% delivery figure
particular to this mesh? Worth repeating this exact analysis on the next room
that gets a full Scenario Runs sweep.

---

## 2026-07-29 — ACH6 flow-field oscillation, a strategic refocus on reduction_pct, and a validated deltaT trick that makes low-ACH runs cheap

A single day's investigation that started as "why is the ACH6/Z6 flow base
taking so long to converge" and ended with a production default change to
how every steady-state Phase 1/2 run budgets its iterations.

### Finding 1 — ACH6's flow field is genuinely, persistently oscillating, not slowly converging

Pulled real per-iteration `p`/`Ux` residuals straight from `log.simpleFoam`
for the ACH6/Z6 flow base: a real, non-decaying oscillation with a ~300-400
iteration period, not noise and not a slow monotonic approach. The
convergence checker (`_is_stable_oscillation`) had never actually rendered a
verdict on this case — it needs `2 x oscillation_window` (default window 4)
chunks of history, and this project's own customized
`flow-max-iterations=1500` made that structurally unreachable (would need
≥4000 iterations just to be *eligible* for a verdict).

### Finding 2 — the oscillation matters far less to reduction_pct than to eACH_uv

Froze the same ACH6 flow field at two different oscillation phases (peak
and trough, via `converge_flow_field(resume=True, ...)`), then ran a full
Phase1(4000)+Phase2(2500) from each frozen state. `eACH_uv_steady_state`
swung ~20% between the two phases (64.79 vs 77.75), but `reduction_pct`
(the room's actual pathogen-reduction percentage) only swung ~1.3 points
(91.52% vs 92.84%). Mechanism: `reduction_pct = 1 - T_ss2/T_ss1` is a
*bounded ratio*, while `eACH_uv` involves `G/(V*T_ss2)` — T_ss2 sits in a
denominator, which amplifies the same underlying noise.

This directly reframed the project's priorities (this is a room/HVAC design
tool, not a metrology instrument): **reduction_pct is the number a user
actually needs** and is robust to flow-field noise the CFD can't fully
eliminate; eACH_uv is an "academic topping" that's inherently noisier and
shouldn't be over-trusted at the precision it's often reported to.

### Finding 3 — the codebase's math exactly matches a published reference

Cross-checked the steady-state model against the user's own co-authored
paper ("Air disinfection... large scale chambers", supplemental S1-3). The
governing equations (`AER=1/theta`, `R=AER+k_S+k_E`, `SS_1=S/(V*R)`,
`SS_2=S/(V*R_UV)`, and both phases' exponential-approach forms) match the
codebase's model exactly. The paper's stated criterion — **"SS_1 is
approached after roughly 4-6 residence times"** — became the theoretical
basis for Finding 4 below (residence time scales as 1/ACH, which is exactly
why low-ACH cases need proportionally longer runs).

### Finding 4 — deltaT can be scaled up for free in Phase 1/2, and it works

`simpleFoam`'s own U/p/k/omega solve has no time-derivative term at all
(pure SIMPLE relaxation) — the *only* place OpenFOAM's pseudo-time step
(`deltaT`) matters is `scalarTransport1`'s bolt-on `T` equation, solved
implicitly (unconditionally stable, no CFL-type limit). So scaling `deltaT`
up costs nothing and risks nothing to the frozen flow field, while letting
`T`'s own buildup traverse the paper's "4-6 residence times" within a fixed,
cheap iteration budget instead of needing more iterations.

Validated on 3 real cases, comparing a 1500/1000-iteration run using scaled
deltaT against a 4000/2500-iteration deltaT=1 baseline (2.67x more real
solver iterations):

| Case | Budget | reduction_pct | eACH_uv |
|---|---|---|---|
| ACH3/Z1.7 | 4000/2500, dt=1 (baseline) | 85.84% | 18.19 |
| ACH3/Z1.7 | 1500/1000, dt=1 (unscaled) | 75.60% | 9.30 |
| ACH3/Z1.7 | 1500/1000, scaled dt | 85.77% | 18.08 |
| ACH3/Z1.7 | 750/500, scaled dt (too short) | 87.90% | 21.79 |
| ACH6/Z6 (oscillating case) | 1500/1000, scaled dt | 92.46% | 73.59 |
| ACH9/Z6 | 1500/1000, scaled dt | 83.26% | 44.75 |

The scaled 1500/1000 run matches the 4000/2500 baseline almost exactly at
ACH3, and ACH6's scaled result (92.46%/73.6) lands squarely inside the
oscillation-driven noise band from Finding 2 (91.5-92.8%/64.8-77.8) —
indistinguishable from the full-budget run given the flow field's own
irreducible noise. Halving the budget again (750/500) undershoots even with
scaling — 1500/1000 is the validated floor, not 750/500.

**Now the production default** (`guvcfd.app_settings`: `deltat-scaling-
enabled=True`, `deltat-effective-fraction=0.7` — measured ACH/eACH runs
below nominal, conservative derating — `deltat-target-fraction=0.995`, ~5.3
residence times). Purely additive on top of the existing iteration-budget
safety margin, so a case that already converges fine at deltaT=1 (typically
higher-ACH) is completely unaffected — see `compute_scaled_delta_t`/
`resolve_phase_delta_ts` and "OpenFOAM settings background.md" for the full
implementation writeup.

### Finding 5 — the same trick does not carry over to decay mode

Decay mode's transient run uses `pimpleFoam`, where U/p *do* have real,
Courant-constrained time-derivative terms (`adjustTimeStep`, `maxCo 0.5`).
Every function object (including `scalarTransport1`) shares OpenFOAM's one
global clock, so `T` can't be given an independently-inflatable timestep the
way `simpleFoam`'s ddt-free U/p allowed in Phase 1/2 — scaling `deltaT` up
for `T` there would mean scaling it up for U/p too, risking real accuracy
loss in the transient flow itself. This also isn't a gap to fix later:
decay mode's whole method is to fit a *real* decay rate against real
elapsed time (`fit_effective_decay_rate`), so blurring time resolution would
bias the very quantity being measured. Decay mode also never had the
"iterations == seconds" conflation this fix addresses in steady state —
its own `_settling_iterations()`-derived `pimple_end_time` is already
consumed in real seconds, not iterations.
