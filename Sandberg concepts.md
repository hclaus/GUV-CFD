# Sandberg (1981) "What is Ventilation Efficiency?" — concepts, cross-checks, and what they mean for GUV-CFD

Source: M. Sandberg, *Building and Environment* 16(2), 123-135 (1981) — the foundational
paper defining ventilation efficiency, age-of-air, and the tracer-gas methods almost every
later paper (including Duque-Daza et al. 2024, reviewed the same day as this note) cites for
its efficiency formula. Read in full 2026-08-02.

This is a companion to `ANALYSIS_LOG.md`'s 2026-08-02 entries (age-field vs. Lagrangian
tracking evaluation) - several of Sandberg's identities turned out to bear directly on that
still-open investigation.

---

## 1. The core concepts, and how they map onto what we already compute

Sandberg formally separates several distinct quantities that are easy to conflate. Mapping
them onto GUV-CFD's own vocabulary:

| Sandberg's concept | Definition | GUV-CFD equivalent |
|---|---|---|
| **Absolute ventilation efficiency**, ε^a (Eq. 3) | `(C(0) - C_j^s) / (C(0) - C_t)` - actual reduction achieved, relative to the theoretical best-possible reduction (all the way down to supply-air concentration) | **Exactly `reduction_pct`** (with C_t=0, ε^a = 1 - T_final/T_initial = our reduction fraction) - already computed, already theoretically grounded, no change needed |
| **Relative ventilation efficiency**, ε'_j / ε̄^r (Eqs. 1-2) | `(C_f^s - C_t) / (C_j^s - C_t)` (local) or `(C_f^s - C_t) / (C̄^s - C_t)` (room-average) - exhaust concentration vs. a point/room-average concentration, at steady state | **Not currently computed anywhere.** This is exactly the formula Duque-Daza et al. cite as "ventilation efficiency" (their Eq. 5) - see §4 |
| **Local age of air**, θ_j (Eqs. 5-6) | First moment of the local pulse-response curve - mean time since air at point j entered the room | Our `age` scalarTransport2 field (Eulerian snapshot) is exactly this concept, computed the same way (see §5 for a maturity caveat just found) |
| **Local ventilation rate**, r_j (Eq. 7) | `1/θ_j` - inverse of local age | Not currently reported, trivial to add (`1/age`) |
| **Local air-exchange rate**, n_j (Eq. 8) | `Q_j/V_j` - flow rate through a control volume ÷ that volume. Always ≥ r_j; equal only under complete local mixing | Not computed - a genuinely different quantity from age, needs local flow-rate estimates we don't currently extract |
| **Nominal air-exchange rate**, n (Eq. 4) | `Q/V_r` - the whole-room ACH | Our nominal `ach` setting, exactly |

## 2. The three transient measurement methods

Sandberg defines and proves the relationship between three ways of running a tracer
experiment:

1. **"Decay" method**: mix the room to a uniform initial concentration (with fans), turn the
   fans off, watch it decay under the real ventilation flow. **This is exactly GUV-CFD's own
   "decay" simulation mode** - `T = uniform 1` everywhere at t=0, then the real (unforced)
   flow field governs the decay. One difference worth noting: Sandberg's protocol uses fans to
   *achieve* the uniform mixed state before release; our CFD just *sets* `uniform 1` directly
   as an initial condition (no fan-mixing artifact to worry about, but also no physical
   analogue for how a real uniform release would actually be achieved).

2. **"Source" method**: hold the *supply air* concentration constant, watch concentration grow
   at various points from zero to steady state. **Not quite the same as GUV-CFD's steady-state
   buildup phase** - Sandberg's method injects contaminant via the *supply/inlet air itself*;
   our steady-state mode injects via an interior source *zone* (near an occupant), with clean
   air still entering at the inlet. Same overall "buildup to steady state" structure, different
   source location - worth being precise about this when citing Sandberg for our own buildup
   runs.

3. **"Pulse" method**: inject a brief tracer burst *into the supply air duct* (upstream of the
   room), watch it grow and decay. Sandberg proves this is theoretically equivalent to the
   decay/source methods via a substitution, and derives two clean, assumption-light identities
   from it (see §3). **Important distinction from this session's own "pulse-at-inlet"
   experiment** (see §6) - Sandberg's pulse enters *through* the inlet boundary condition, not
   as a volumetric initial condition already inside the room.

## 3. Two exact identities (proof-based, not empirical) - genuinely useful cross-checks

Both of these hold for *any* incompressible flow field, regardless of internal mixing pattern
(short-circuiting, dead zones, recirculation all integrate out correctly) - they follow purely
from mass/tracer conservation over the whole domain, the same style of argument used for this
session's own washout-fraction identity (`ANALYSIS_LOG.md`, 2026-08-02).

**3a. Residence time at the exhaust (Eq. 21a):**

```
θ_f = V_r / Q = 3600 / ACH_nominal   [seconds, ACH in 1/hr]
```

The mean age of air *specifically at the exhaust* always equals the room volume divided by the
volumetric flow rate - exactly, not approximately, regardless of mixing quality. This is a
free, rigorous sanity check for both the CFD's own `age` field and the Lagrangian tracker's
exit-time statistics.

**3b. Pulse-area conservation (Eq. 18):**

```
∫ C^tr(τ) dτ = m/Q = constant, for EVERY point in the room
```

For a true supply-duct pulse release, the *area* under the transient-concentration curve at
any point is the same constant, independent of location. A cheap, powerful sanity check if we
ever implement a genuine inlet-duct pulse (see §6).

### Checked directly against our own data (2026-08-02)

Using the already-completed `patient_ward_4B1_v10_simdcay` case (nominal ACH=6, room volume
≈36 m³, so θ_f theory = 3600/6 = **600.0 s exactly**):

| Source | Flux-weighted age at outlet | vs. theory |
|---|---|---|
| CFD `age` field (scalarTransport2), read at t=500s | 310.2 s | **51.7%** |
| Lagrangian tracker mean exit time (N=300, diffusion-on, exited particles) | 579.7 s | **96.6%** |

**The CFD age field is substantially under-matured at typical run durations - a genuine,
previously-unflagged finding.** The room's own age statistics confirm why: at t=500s, the
room-average age is only 340.5s and the *maximum* age anywhere in the room is 455.5s - still
less than the 600s nominal residence time, at a run duration that is itself shorter than one
nominal residence time. Ruled out as a calculation artifact: checked all 16 outlet faces for
backflow (none - Ux ranges 0.21-0.48 m/s, all genuine outflow). The age scalar simply hasn't
had time to reach its statistically-steady spatial distribution - `age(x,t)` needs several
nominal residence times to saturate, the same way a first-order system approaches its
asymptote, and our decay-mode run durations are sized for the *T*/eACH_uv target, not for age-
field maturity.

**This directly affects this session's earlier age-field-snapshot dose work** - not just the
"snapshot vs. trajectory" conceptual gap already documented, but the snapshot itself was read
before it had converged. It does *not* directly affect the Lagrangian tracker's own mean exit
time, which (reassuringly) already tracks the exact theoretical value to within ~3%, and would
likely track it even closer once the still-open survivorship/trapped-particle handling is
tightened further.

## 4. Which measure is "best"? Sandberg's own experimental answer

Sandberg's own measurements (in a real 3.6×4.2×2.7m test room, N₂O tracer, varying supply
overtemperature and nominal ACH) are directly relevant to a question this project has already
been circling (`ANALYSIS_LOG.md`'s decay-vs-steady-state entries, and the ~200% materiality
threshold noted in project memory):

> "The area under the curve is the best measure of efficiency, because all information about
> the ventilation process is taken into account... In systems where large variations in the
> local ventilation efficiency exist, the slope should not be used as a measure."

His own data (Table 6) shows the decay-rate/slope method (his Method 1, matching our own decay
mode) and the concentration-ratio method (his Method 2) agree well - *except* specifically at
**low nominal ACH under near-isothermal conditions**, exactly where he attributes the
discrepancy to unstable flow patterns, not the methods themselves. **This is an independent,
much older piece of literature corroborating this project's own observed low-ACH sensitivity**
(the y+/mesh-grading investigation and the decay-vs-steady-state gap already in
`ANALYSIS_LOG.md`) - worth citing there directly.

GUV-CFD's `eACH_uv_effective`/`lambda_total_effective_per_s` are exactly the type of measure
(a fitted exponential decay rate - Sandberg's "slope") that his own experiments found least
robust, particularly in the regime (low ACH) where this project has already independently
found the most noise.

## 5. Suggested improvements, ranked by effort

**Cheap, no new simulation needed:**

1. **Report Sandberg's relative ventilation efficiency, ε̄^r = (C_f - C_t)/(C̄ - C_t)**, as a
   standard output alongside `eACH_uv_effective`. The outlet-average data already exists for
   every completed run (`monitoring.write_vol_average_dict`'s default `patches=("outlet",)` -
   the same `postProcessing/outletAverage/.../surfaceFieldValue.dat` file discovered while
   building the pulse-at-inlet experiment) - this metric is a few lines of reading code away,
   not a new simulation.
2. **Report the same formula locally at each monitoring point** (ε'_j) - the monitoring-point
   infrastructure already exists; same formula, substitute the point's own concentration.
3. **Add Sandberg's θ_f = V_r/Q maturity check as a standard diagnostic** whenever an
   age-of-air or RTD-derived result is reported - flag/warn if the run duration is shorter than
   a few nominal residence times, or if the outlet-age-vs-theory ratio is far from 1. Directly
   would have caught the maturity gap found in §3 before it fed into other analysis.
4. **Report `age`'s own local ventilation rate 1/age** - trivial, currently unused.

**Moderate effort:**

5. **Implement an area-under-the-curve efficiency estimator** (Sandberg's ε̂, Eq. 34 - ratio of
   the integrated exhaust curve to the integrated local/room curve) as a cross-check alongside
   the existing decay-rate-fit `eACH_uv_effective`, particularly for low-ACH cases where
   Sandberg's own data (and ours) shows the slope-based measure getting noisiest. A trapezoidal
   integral of already-recorded curves - no new simulation.
6. **When age-of-air/RTD results are actually needed** (not just the T/eACH_uv target), run
   decay mode for several nominal residence times rather than whatever duration the adaptive
   UV/eACH-targeting logic picks - the two have different convergence requirements and
   shouldn't share one run-duration heuristic uncritically.

**A correction to this session's earlier pulse-at-inlet experiment:**

7. The pulse-at-inlet experiment (`pulse_at_inlet_experiment.py`, `ANALYSIS_LOG.md` same day)
   used a **volumetric initial condition** (a sphere of T=1 already sitting inside the room,
   near the inlet) - not Sandberg's Method 3, which injects the pulse **through the inlet
   boundary condition itself** (a brief spike in the inlet's fixedValue T, room starting at
   T=0). These are meaningfully different experiments: the volumetric IC's pulse already
   occupies room volume and sits partly outside the strongest jet-influenced zone depending on
   radius (which is what drove the earlier radius-sensitivity finding); an inlet-duct pulse
   would enter the domain exactly the way continuously-supplied contaminated air (and every
   Lagrangian-tracked particle) does. **Recommend re-running the comparison this way** - it's a
   more faithful match to both Sandberg's own theory and the Lagrangian tracker's seeding
   assumption, and might clarify (not necessarily resolve) the still-open Euler-vs-Lagrangian
   discrepancy from `ANALYSIS_LOG.md`. Sandberg's pulse-area-conservation identity (§3b) would
   also become available as a free correctness check on that new experiment.

## 6. Summary

Sandberg's 1981 paper isn't just historical context for the efficiency formula the Duque-Daza
paper cites - it supplies exact, assumption-light identities that double as free validation
tools for work already done this session, and its own experimental conclusions independently
corroborate this project's separately-observed low-ACH instability. The most concrete result
of this reading: **the CFD's own age-of-air field, as read at typical decay-run durations, is
only ~52% matured toward its theoretical steady value** - a real, previously-unflagged gap that
sits upstream of (and partly explains the uncertainty around) this session's age-field and
Lagrangian dose-comparison work.
