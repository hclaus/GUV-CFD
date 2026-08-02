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

---

## 2026-08-01 — Wall-function y+ and inlet turbulence-intensity: one real (small) sensitivity, one negligible, no bug found

### Background — how `scalarTransport` produces exponential (not linear) UV decay

`cellzones.write_fvoptions()` writes one `scalarSemiImplicitSource` per
UV-rate bin, each with `injectionRateSuSp { T (0 -k); }` — an `(Su Sp)`
pair OpenFOAM adds to the transport equation as `source = Su + Sp*field`.
Here `Su=0` and `Sp=-k`, and critically `Sp` multiplies the field value
itself (the local concentration `C`), not a fixed number — so the sink term
is `-k*C`, proportional to whatever's currently in the cell. The governing
per-cell ODE is `dC/dt = -k*C`, which integrates to `C(t) = C0*e^(-kt)` -
exponential decay. Had `k` instead been placed in `Su` (a constant,
independent of `C`), the sink would remove scalar at a *fixed* rate
regardless of remaining concentration, giving linear decay (and eventually
negative `C`). OpenFOAM discretizes `Sp` implicitly (added to the solver
matrix's diagonal each timestep), so the solver is literally integrating
`dC/dt=-kC` cell-by-cell, coupled with whatever advection/diffusion moves
scalar between cells.

Per-cell `k = Z * fluenceRate * 1e-3` (`fluence.compute_inactivation_rate`)
is continuous, but `scalarSemiImplicitSource` only accepts one uniform
coefficient per `cellZone` — so `cellzones.bin_decay_rates()` log-bins `k`
into `nbins` groups (geometric mean of each bin's `[lo,hi)` edges as the
representative rate), each becoming its own `cellZone`/source block. This
is a discretization approximation, not something found replicated in the
UV-disinfection CFD literature — published Eulerian models typically apply
a genuinely continuous per-cell reaction rate (e.g. a UDF in commercial
code, or `codedFvOption`/`fvm::Sp(k_field, C)` in OpenFOAM), and Lagrangian
models sidestep the issue entirely by integrating dose along particle
tracks and applying a dose-response kinetic model afterward - see
Ho/Zhu/Malley-type UV reactor CFD papers.

Turbulent diffusion is explicitly on: `alphaD=1`/`alphaDt=1` in the
`scalarTransport1` functionObject give effective diffusivity
`D = alphaD*nu + alphaDt*nut`, with `nut` computed by `kOmegaSST` from its
own `k`/`omega` transport equations (an algebraic closure,
`nut = a1*k/max(a1*omega, b1*F2*sqrt(2*S:S))` - not a value chosen
directly anywhere). One cited paper (UV Reactor Performance Modeling by
Eulerian and Lagrangian Methods, ES&T) found Eulerian/Lagrangian UV models
only converge if turbulent diffusion is *dropped* from the Eulerian
equation - since this pipeline keeps it on (physically correct for a
turbulently-mixed room), this Eulerian result should NOT be expected to
match a Lagrangian particle-tracking model unless that model also carries
equivalent turbulent dispersion.

### This session's investigation

Triggered by that same walkthrough, which surfaced two further unverified
modeling assumptions: (1) the near-wall mesh has
no boundary-layer grading (`mesh_gen.block_mesh_dict` uses uniform
`simpleGrading (1 1 1)`), so first-cell y+ is whatever the background
`cell_size` happens to produce, and (2) `initial_fields.py`'s inlet `k`/`omega`
(`0.0039`/`5.43`) are hardcoded constants ported from a single reference
case's 0.278 m/s inlet velocity, never rescaled for
`compute_inlet_velocity`'s ACH-derived velocity in other runs. **Question**:
does either have a significant effect on results?

**Method**: paired A/B sensitivity tests on `patient_ward_4B1_v10_simdcay`'s
ACH3 geometry (Z1.7_ACH3's mesh/opening geometry, simplified to a uniform
"direct jet" inlet BC rather than the real per-face ceiling-diffuser BC, to
keep the test tractable). Built otherwise-identical case pairs differing only
in the flagged variable, ran flow-only convergence (`simpleFoam` via
`converge_flow_field`, UV/T never touched), compared y+ (OpenFOAM's `yPlus`
function object) and bulk flow-field stats (`volAverage(p)`, `mag(U)`).

### Finding 1 — y+ on real production runs scales strongly with ACH

`postProcess`/`-postProcess -func yPlus` on the completed `Z1.7_ACH3/6/9`
decay runs: patch-average y+ ~9-22 at ACH3 (buffer layer, below the ~30
threshold where `nutkWallFunction`/`omegaWallFunction` are valid), ~19-45 at
ACH6, ~33-68 at ACH9 (mostly valid). Confirmed no boundary-layer/y+-targeted
meshing exists anywhere in this pipeline — first-cell height is purely a
byproduct of the uniform background mesh and whatever local velocity
develops.

### Finding 2 — near-wall mesh coarsening is a real, ~4% effect (not fixed, left as-is)

Added an optional `wall_cell_size` parameter to `mesh_gen.block_mesh_dict()`
(new `_direction_grading()` helper: 3-segment multi-grading per direction, a
single coarser cell at each wall, uniform interior otherwise — default
`None` preserves today's behavior exactly for every existing caller, not
wired into `write_mesh_dicts()`/`setup_case()`). A 0.3m wall cell (vs. 0.1m
interior) raised ACH3 y+ averages from ~15-24 to ~61-142. Comparing the two
otherwise-identical converged flow fields: `volAverage(p)` +3.8%, mean
`|U|` −3.9%, max `|U|` +2.5% — a real, consistent, same-direction shift
across independent metrics, not noise.

### Finding 3 — inlet k/omega rescaling is negligible

Rebuilt the same case with `k`/`omega` rescaled for the actual inlet
velocity using the *same* implicit turbulence-intensity/mixing-length
assumption already baked into the hardcoded reference values (reverse-
engineered from the 0.278 m/s reference case: since
`k = 1.5(IU)²` and `omega = sqrt(k)/(Cμ^0.25 L)` with `I`, `L` held fixed,
`k` scales as `(U_new/U_ref)²` and `omega` as `(U_new/U_ref)`ⁱ — giving
k: 0.0039→0.00674, omega: 5.43→7.14 at ACH3's 0.3655 m/s). Result:
`volAverage(p)` +0.05%, mean `|U|` −1.2%, y+ shifted <2 units per patch
(~6-9% on the smallest patches, plausibly within the convergence method's
own 1%-per-chunk tolerance noise). Interior turbulence in this recirculating
room flow is dominated by shear production off the mean flow (walls, jets),
not by whatever `k`/`omega` walked in at a small inlet opening — consistent
with why this barely propagates past the immediate opening.

### Conclusion — no bug; both assumptions quantified, left as-is by decision

Neither issue is a bug that silently produces wrong answers — both are
known, now-quantified simplifications. Effect sizes (≤4%) are two orders of
magnitude below the ~200% eACH differences this project uses decay-vs-
steady-state comparisons to detect (see `feedback_materiality_threshold`
memory), so **decision (2026-08-01, user): leave both as-is** — not worth
the added meshing complexity/runtime for a signal this project doesn't
currently need resolved. Revisit only if a future analysis needs
sub-5%-level precision from a single ACH/mesh configuration (the near-wall
grading code already exists in `mesh_gen.py` if so — see Finding 2).

**Scratch artifacts** (not part of the production pipeline, safe to delete):
`_yplus_test_uniform_ACH3`, `_yplus_test_graded_ACH3`, `_kw_test_scaled_ACH3`
under `patient_ward_4B1_v10_simdcay/`, alongside the real runs.

### Literature check — has anyone else reported Z/k (or eACH) differing between decay-mode and steady-state-mode measurement/simulation?

Web search, prompted by this project's own core comparison (decay vs.
steady-state eACH, target difference ~200%). No single paper was found that
runs the *identical* chamber/system through both a decay protocol and a
constant-generation (steady-state) protocol and reports a head-to-head
numeric rate-constant comparison — that specific clean A/B test looks like
a genuine gap in the published literature, not something this search missed
by using the wrong terms (multiple phrasings were tried). What *is*
well-documented is several adjacent strands of evidence that the two
methodologies plausibly diverge:

- **Decay and steady-state ("constant generation") are explicitly two
  different, established protocols** in the upper-room UVGI literature —
  the steady-state approach (comparing survival fractions with/without UV
  at continuous generation) traces to Rudnick & First's dosimetry model:
  First MW, Rudnick SN, Banahan KF, Vincent RL, Brickner PW (2007),
  "Fundamental Factors Affecting Upper-Room Ultraviolet Germicidal
  Irradiation — Part I: Experimental," and Rudnick SN, First MW (2007),
  "...Part II: Predicting Effectiveness," both *J. Occup. Environ. Hyg.*
  No paper surfaced that reports both protocols' rate constants for the
  same setup, but the fact that both are standing, independently-used
  methods (rather than one being treated as a validation check on the
  other) is itself notable.

- **A near-identical methodology question, for generic particulate air
  cleaners rather than UV specifically**: Haratian, Chittoo, Subramanian,
  Verma, Heidarinejad, Stephens & Sherman (2025), "An integral
  approach to quantifying equivalent clean airflow rates of indoor air
  cleaning devices from pollutant injection and decay tests,"
  *Building and Environment* — explicitly built to address the fact that
  injection/continuous (steady-state-like) and decay first-order rate
  constants "can lead to errors, biases, and/or high uncertainties" when
  compared naively, and proposes a reconciliation method. Directly
  analogous structure to this project's decay-vs-steady-state comparison,
  just for particles/adsorption rather than UV inactivation specifically.
  https://www.sciencedirect.com/science/article/abs/pii/S0360132325011035

- **A concrete, quantified example of methodology changing the fitted
  rate/susceptibility constant by ~2x**: "Improved estimates of 222 nm
  far-UVC susceptibility for aerosolized human coronavirus via a validated
  high-fidelity coupled radiation-CFD code" (*Scientific Reports* 11,
  19930, 2021; WYVERN coupled radiation-CFD code) — single-exponential
  decay-curve
  fitting gave a susceptibility constant of k≈5.6 cm²/mJ, while a
  dose-resolved (bi-exponential, CFD-informed dosimetry) analysis of the
  same underlying data gave 12.4 cm²/mJ at low doses — roughly 2.2x higher.
  Not a decay-vs-steady-state comparison per se, but direct evidence that
  *how* the rate constant is extracted from data materially changes its
  value, in this exact application domain.
  https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8497589/

- **Continuous-flow UV-C reactor kinetics are reported to genuinely change
  character between the transient and steady-state regimes**, not just
  differ in fitted value: a reactor-engineering study of continuous UV-C
  processing found inactivation kinetics moving from first-order during
  unsteady-state operation to near-zero-order once steady state was
  reached — i.e., decay-phase and steady-state-phase kinetics were found to
  belong to different kinetic regimes, not just different noise levels of
  the same one.
  https://www.sciencedirect.com/science/article/abs/pii/S146685642100254X

- **CFD methodology papers explicitly flag the steady-state assumption as
  unverified for UV reactors**: "Standard Methodology for Transient
  Simulations of UV Disinfection Reactors," *J. Environ. Eng.* 143(3),
  2016 — notes most UV-reactor CFD historically assumed steady-state
  because transient effects were *believed* not to matter, then argues
  that assumption needs explicit justification, not just adoption by
  convention. https://ascelibrary.org/doi/10.1061/%28ASCE%29EE.1943-7870.0001153

- **The theoretical anchor for this whole question** — Blatchley, Belenky,
  Claus, DeGroot, Hadizade, Hiwar, Noakes, Shorno & Williamson (2026),
  "Large-scale chamber tests of in-room germicidal ultraviolet (GUV)
  systems: Review and best practices," *Building and Environment* 294:
  114357 (open access), plus its Supplemental Information (SI-3:
  "Governing Equations for Simulation of IAQ Dynamics Under Test
  Conditions") — both read in full, PDF/docx supplied by the user,
  2026-08-01. A 2026 field-consensus review from an ad-hoc committee (2nd
  International Congress on Far UV Science and Technology, 2024) defining
  best practices for exactly the steady-state/static-decay/dynamic-decay
  chamber-test methodology this project uses. (Note: co-author Holger
  Claus, A3Lighting Consulting, appears to match this project's own
  author/consultancy.)

  Worked through the SI-3 derivation in full: for a well-mixed chamber
  with source rate S, volume ∀, lumped non-UV loss rate `R = AER+k_S+k_E`,
  and UV-on lumped loss rate `R_UV = R + k·E'_avg` — steady state gives
  `SS1 = S/(R·∀)` and `SS2 = S/(R_UV·∀)` (Eq. SI-6, SI-13); dynamic decay
  from SS1 gives `C(t')=SS1·exp(−R·t')` (UV off, Eq. SI-17) and
  `C(t')=SS1·exp(−R_UV·t')` (UV on, Eq. SI-19). **The same R and R_UV
  govern both the steady-state ratio and the decay-phase exponential — not
  an approximation, a direct mathematical consequence of the well-mixed
  model.** So under that model, decay-mode and steady-state-mode eACH_UV
  are the *same quantity by construction*, and a large observed gap
  between them (like this project's own ~200%) is theoretically a
  signature of departure from well-mixed conditions (or a
  methodological/fitting artifact) — not evidence that the two protocols
  measure genuinely different physical quantities. This is the theoretical
  reason Miller & Macher's real well-mixed case (below) landed within
  ~10%. Section 11.5 of the main text makes the same point qualitatively —
  local UV reaction rates can never be truly spatially uniform (fluence
  fields have strong built-in gradients), but *time-averaged* sampling
  over a typical 2–20 min window can still approximate the well-mixed
  model reasonably well. Section 13 explicitly lists "standardization of
  mixing-behavior characterization" and "a standard method for
  quantification of UV-C disinfection kinetics for aerosolized challenge
  agents" as open, field-wide gaps — i.e., this project's own core
  uncertainty (is Z/k right, why do decay and steady-state differ) is a
  recognized unsolved problem in the field, not a project-specific
  shortcoming.
  https://doi.org/10.1016/j.buildenv.2026.114357

- **The closest thing found to a genuinely matched empirical
  decay-vs-steady-state comparison** — Miller SL, Macher JM (2000), "Evaluation of a
  Methodology for Quantifying the Effect of Room Air Ultraviolet
  Germicidal Irradiation on Airborne Bacteria," *Aerosol Science &
  Technology* 33(3): 274–295 (read in full, PDF supplied by the user,
  2026-08-01). Same 36 m³ room, same *B. subtilis* spore aerosol, same
  single unlouvered lamp, tested by both protocols (BS-s1 steady-state vs.
  BS-d2 decay). Steady-state: effectiveness E=56% at 2 ACH ventilation.
  Decay: ACH_UV=3.8 h⁻¹ directly fitted, against a decay-derived non-UV
  removal rate ACH_V+ACH_O=2.7 h⁻¹. **Converting E to an equivalent ACH_UV
  for direct comparison** (derived here from the paper's own model, Eq.
  2–4 — not something the paper computes itself):
  `ACH_UV = (ACH_V+ACH_O) × E/(1−E) = 2.7 × 0.56/0.44 ≈ 3.4 h⁻¹` — within
  ~10% of the decay method's directly-fitted 3.8 h⁻¹. Genuine, fairly
  close agreement for a well-controlled, well-mixed single-lamp case —
  real evidence against assuming a large decay-vs-steady-state gap is
  inherent to the methodology itself.

  But the same paper also documents decay-method fragility directly: the
  *two-lamp* decay run gave ACH_UV=2.3 h⁻¹ — *lower* than the one-lamp
  run's 3.8, despite more UV power. The authors call this "an unexpected
  result," attributed to a poor curve fit (R²=0.870 vs. 0.975–0.998 for
  the other decay runs); fitting only the first two data points instead
  gives 6.5 ACH_UV, "approximately twice that observed with one lamp" —
  matching physical expectation far better. Their own stated
  methodological opinion: "effectiveness is best used with the
  steady-state method as it is independent of time and mixing affects.
  Equivalent air-exchange rate should be used with the decay method
  provided mixing is ensured." Also cites a third, independent
  methodology for context: Salie et al. (1995) single-pass bioassay,
  translated to steady-state-equivalent effectiveness of 13%/6%/56% for
  the same three organisms in a similarly-sized room.
  https://doi.org/10.1080/027868200416259

- McDevitt, Milton, Rudnick & First (2008), "Inactivation of Poxviruses by
  Upper-Room UVC Light in a Simulated Hospital Room Environment," *PLoS
  ONE* 3(9): e3186 (also read in full, 2026-08-01; an earlier pass from
  search snippets alone had mischaracterized it — conflated a
  within-decay-method fan-mixing effect with a decay-vs-steady-state
  comparison, and wrongly treated its own Table 2 as an unverified
  "McDevitt et al." table from a *different* source). Ran both a decay
  protocol (single UVC fixture; eACH_UVC 7 no fan → 92 with fan mixing,
  Table 1) and a steady-state protocol (1/4 fixtures × 2/6 ACH ×
  summer/winter; eACH_UVC 18–1000, single-fixture subset 18–150, Table 2)
  in the same chamber with the same fixtures — a second, less-matched
  same-chamber comparison (decay only varied fan/heat-boxes; steady-state
  only varied ACH/season; temperature/RH protocols differ too). For the
  one roughly-comparable pair of conditions (single fixture, well-mixed),
  the decay result (87–92) again sits within the steady-state
  single-fixture range (18–150) — same order of magnitude, consistent with
  Miller & Macher's closer-matched result above.
  https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0003186

- Nicas M, Miller SL (1999), "A multi-zone model evaluation of the
  efficacy of upper-room air ultraviolet germicidal irradiation," *Appl
  Occup Environ Hyg* 14: 317–28 — the steady-state-over-decay
  recommendation both papers above cite; not yet read first-hand, only as
  cited by them. Related: Nicas M (1996), "Estimating Exposure Intensity
  in an Imperfectly Mixed Room," *Am Ind Hyg Assoc J* 57: 542–550 — cited
  by Miller & Macher as the source for why poor mixing specifically
  corrupts decay-method ACH determinations.

**Takeaway** (revised 2026-08-01 after reading Blatchley et al. 2026,
Miller & Macher 2000, and McDevitt et al. 2008 in full): this question now
has a real theoretical anchor, not just adjacent empirical analogies.
Blatchley et al.'s SI-3 derivation shows that under the well-mixed model,
decay-mode and steady-state-mode eACH_UV are **the same quantity by
mathematical construction** — the identical lumped rate constant R_UV
governs both the steady-state ratio SS1/SS2 and the with-UV decay
exponential. So a large observed decay-vs-steady-state gap is, by that
theory, necessarily a signature of departure from well-mixed conditions
(or a methodological/fitting artifact) — not evidence that the two
protocols are measuring genuinely different physical quantities. Miller &
Macher's real, well-controlled single-lamp case empirically bears this
out: decay and steady-state landed within ~10% of each other. The same
paper also demonstrates exactly how that agreement breaks — poor mixing
and/or a noisy, sparse decay curve (their two-lamp run's ACH_UV coming out
*lower* than the one-lamp run's, a result they call unexpected and
attribute to a bad curve fit) — and states outright that decay-derived
ACH_UV is reliable only "provided mixing is ensured," unlike steady-state
effectiveness. McDevitt et al. 2008 adds a second, less-matched but
consistent same-chamber comparison. Combined with the other adjacent
evidence (methodology papers built specifically to reconcile decay vs.
injection/steady-state discrepancies for the analogous particle-cleaner
case; a documented ~2x swing in fitted UV susceptibility depending on
extraction method; documented regime-change in reactor kinetics between
transient and steady operation; explicit literature caution about the
steady-state CFD assumption for UV reactors): no paper was found that
isolates protocol as the *only* varied condition on a real physical
chamber — that specific clean experimental test still looks like an open
gap. But this project's own ~200% decay-vs-steady-state signal is no
longer an unprecedented result to puzzle over in the abstract — theory
says the two should match under good mixing, so the size of the gap is
itself a measurement of how far this CFD room's mixing (or the two modes'
fitting/methodology) departs from that ideal, which is exactly what this
session's own y+/mesh-grading investigation (above) was probing directly.
