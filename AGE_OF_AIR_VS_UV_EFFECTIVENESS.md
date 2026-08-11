# Age-of-air theory (ASHRAE 129) vs. UV effectiveness

Research note (2026-08-07): why the standard ventilation "air-change
effectiveness" framework can't be directly combined with UV dose/
effectiveness, and what that implies for building general ACH-vs-eACH
engineering formulas beyond the well-mixed assumption.

## ASHRAE 129 / Sandberg's age-of-air framework

Core quantities (verified against the standard's own summary and the
underlying Sandberg theory it's built on):

- **Local mean age of air** — the average time since the air at a point
  entered the room, from a tracer-gas decay curve:

  ```
  tau_p_bar = Integral[0, inf] C(t) dt / C(0)
  ```

  (the area under the normalized decay curve at that point). A step-up/
  buildup variant exists too (ISO 16000-8), same underlying idea.

- **Nominal time constant**: `tau_n = V / Q` (room volume / outdoor air
  supply rate) — this is just `1/ACH` in different units.

- **Air Change Effectiveness**: `E = tau_n / tau_p_bar` — compares the
  actual local age to what a *perfectly mixed* room's age would be.
  E = 1 (100%) everywhere under perfect mixing; E < 1 where air is
  stale/short-circuited, E > 1 where it's fresher than average.

- **Air Exchange Efficiency** (Sandberg's related-but-distinct index):
  `epsilon_a = tau_n / (2 * tau_bar)` — the factor of 2 rescales it so
  perfect mixing = 50% and ideal piston/displacement flow = 100% (the
  theoretical ceiling). Easy to confuse with Air Change Effectiveness
  above — same family, different normalization, don't conflate them.

Sources:
- [ANSI/ASHRAE 129-1997 (RA 2002) - Measuring Air-Change Effectiveness](https://webstore.ansi.org/standards/ashrae/ansiashrae1291997ra2002)
- [A new method for air exchange efficiency assessment including natural and mixed mode ventilation (PMC)](https://pmc.ncbi.nlm.nih.gov/articles/PMC8511897/)

## Why this can't be directly combined with UV effectiveness

This isn't a gap that just hasn't been bridged yet - it's structural.

Age of air is a **passive, first-moment** statistic of the residence-time
distribution (RTD): it only encodes *how long* a parcel of air has been
in the room, with zero information about *where* it spent that time.

UV inactivation is a **reactive** process: the dose a parcel accumulates
is `Integral k(x(t)) dt` along its *actual path*, where `k` is the local
fluence rate - a field that's often extremely concentrated right at the
lamp. Two rooms can have identical mean age of air and very different UV
performance, if one happens to route air through the high-fluence zone
and the other routes it around. Age of air cannot see that difference -
it was never designed to carry spatial/reactive information, only a time
average.

**Concrete evidence already in this project**: the fan-speed investigation
in `MIXING_METRICS.md` found spatial CoV (a mixing-uniformity metric) and
UV mixing efficiency moving in *opposite* directions as fan speed
increased - better mixing, worse UV performance - because faster cycling
swept air past the lamp zone rather than letting it linger there long
enough to accumulate dose. That's exactly the decoupling the theory
predicts, observed directly in a real CFD comparison.

## What this project already has toward a reactive generalization

- `age_analysis.py` - computes the Eulerian age field (via the `age`
  scalarTransport field) and converts it into a proper residence-time
  distribution E(t). This *is* the direct ASHRAE-129/Sandberg quantity,
  already wired into the pipeline.

- `lagrangian_tracking.py` - the theoretically-correct reactive
  generalization: seeds particles at the inlet (flux-weighted) and
  integrates each one's *true* full-trajectory dose before it exits,
  following Blatchley's segregated-flow model. This is the right
  mathematical bridge between age-of-air theory and a spatially-varying
  reactive sink - dose = accumulated `k` along the real path, not just
  local `k` times local age (which is what a naive Eulerian
  `dose[x] = fluenceRate[x] * age[x]` approximation gives, and which
  systematically undercounts a parcel's dose since it hasn't finished its
  journey yet at any given snapshot point - see `dose_distribution.py`'s
  own docstring).

## Practical note (2026-08-07): Lagrangian tracking is theoretically right but too costly to be the workhorse

Running Lagrangian particle tracking with enough particles/accuracy to be
trustworthy is not feasible at PC scale, especially for the kind of large
multi-parameter sweep (ACH x diffuser type x room shape x lamp height x
...) needed to discover general trends - it's expensive per case, and a
trend-discovery campaign needs many cases.

**Practical path forward**: treat Lagrangian tracking as a *validation*
tool, not the main one - run it on a small number of representative
cases to characterize/bound the error of the much cheaper Eulerian
approximation (`dose_distribution.py`'s age x fluence-rate estimate, or a
refined version of it), then rely on the validated Eulerian approach for
the actual large-scale trend-discovery sweep. The research goal (general
ACH-vs-eACH relationships, ideally better than the well-mixed assumption)
should be pursued by correlating CFD-measured `mixing_efficiency`/
`eACH_uv_effective` against Eulerian RTD/dose-distribution *shape*
statistics (not just a single mean-age number, which - per the section
above - can't see the UV-relevant structure on its own), not by trying to
retrofit ASHRAE 129's own effectiveness index (reaction-blind by
construction) or by running Lagrangian tracking at scale.
