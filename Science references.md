# Science references

Literature gathered while investigating this codebase's modeling choices —
see `ANALYSIS_LOG.md` (2026-08-01 entry) for the discussion each group of
references supports. Titles/journals/years are as reported by the source;
where an author list could not be independently verified, that's noted
rather than guessed.

## UV-CFD modeling approaches (fluence field, reaction-rate source terms, Eulerian vs. Lagrangian)

- Computational fluid dynamics (CFD) modeling of UV disinfection in a
  closed-conduit reactor — *Chemical Engineering Science*.
  https://www.sciencedirect.com/science/article/abs/pii/S0009250911004295

- "UV Reactor Performance Modeling by Eulerian and Lagrangian Methods" —
  *Environmental Science & Technology*. Finding used in this project:
  Eulerian and Lagrangian UV models only converge to the same answer if
  turbulent diffusion is dropped from the Eulerian transport equation.
  https://pubs.acs.org/doi/10.1021/es051006x

- "Integrated UV Disinfection Model Based on Particle Tracking".
  https://www.researchgate.net/publication/245299857_Integrated_UV_Disinfection_Model_Based_on_Particle_Tracking

- "UV Disinfection – Computational Fluid Dynamics Modeling" — Sandia
  National Laboratories.
  https://www.sandia.gov/cfd-water/uv-disinfection/

- "Performance evaluation of the UV disinfection reactors by CFD and
  fluence simulations using a concept of disinfection efficiency".
  https://www.researchgate.net/publication/250142410_Performance_evaluation_of_the_UV_disinfection_reactors_by_CFD_and_fluence_simulations_using_a_concept_of_disinfection_efficiency

- "Standard Methodology for Transient Simulations of UV Disinfection
  Reactors" — *Journal of Environmental Engineering* 143(3), 2016. Argues
  the common steady-state CFD assumption for UV reactors was adopted by
  convention, not independently verified.
  https://ascelibrary.org/doi/10.1061/%28ASCE%29EE.1943-7870.0001153

- OpenFOAM `CodedFvSource` API reference (per-cell, non-binned
  `fvm::Sp()` source terms — the alternative to this codebase's
  log-binned `cellZone` approach).
  https://www.openfoam.com/documentation/guides/latest/api/CodedFvSource_8H_source.html

- OpenFOAM `fvOptions` User Guide.
  https://www.openfoam.com/documentation/guides/latest/doc/guide-fvoptions.html

## Decay-mode vs. steady-state-mode rate constants (Z / k / eACH)

- First MW, Rudnick SN, Banahan KF, Vincent RL, Brickner PW (2007),
  "Fundamental Factors Affecting Upper-Room Ultraviolet Germicidal
  Irradiation — Part I: Experimental," *Journal of Occupational and
  Environmental Hygiene*.
  https://www.researchgate.net/publication/6440908_Fundamental_Factors_Affecting_Upper-Room_Ultraviolet_Germicidal_Irradiation-Part_I_Experimental

- Rudnick SN, First MW (2007), "Fundamental Factors Affecting Upper-Room
  Ultraviolet Germicidal Irradiation — Part II: Predicting Effectiveness,"
  *Journal of Occupational and Environmental Hygiene*. Source of the
  steady-state (constant-generation) room-average dosimetry model.
  https://www.researchgate.net/publication/6373671_Fundamental_Factors_Affecting_Upper-Room_Ultraviolet_Germicidal_Irradiation-Part_II_Predicting_Effectiveness

- Haratian S, Chittoo PDC, Subramanian PSG, Verma V, Heidarinejad M,
  Stephens B, Sherman M (2025), "An integral approach to quantifying
  equivalent clean airflow rates of indoor air cleaning devices from
  pollutant injection and decay tests," *Building and Environment*.
  Structurally the same methodology question as this project's
  decay-vs-steady-state comparison, for particulate air cleaners rather
  than UV — built specifically because naive comparison of injection
  (continuous) and decay first-order rate constants "can lead to errors,
  biases, and/or high uncertainties."
  https://www.sciencedirect.com/science/article/abs/pii/S0360132325011035

- "Improved estimates of 222 nm far-UVC susceptibility for aerosolized
  human coronavirus via a validated high-fidelity coupled radiation-CFD
  code" (WYVERN code), *Scientific Reports* 11, 19930 (2021). Reports a
  single-exponential decay-curve fit giving susceptibility k≈5.6 cm²/mJ,
  vs. ≈12.4 cm²/mJ at low doses from a dose-resolved bi-exponential/CFD
  analysis of the same underlying data — ~2.2x difference purely from
  extraction method.
  https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8497589/ (also:
  https://www.nature.com/articles/s41598-021-99204-0)

- "A reactor engineering approach to describe bacterial inactivation
  during continuous UV-C light processing" — reports inactivation
  kinetics shifting from first-order during unsteady-state operation to
  near-zero-order once steady state is reached, i.e. a genuine kinetic
  regime change between transient/decay-like and steady-state operation,
  not just a noisier estimate of the same constant.
  https://www.sciencedirect.com/science/article/abs/pii/S146685642100254X

- Blatchley ER III, Belenky JV, Claus H, DeGroot CT, Hadizade A, Hiwar
  WFM, Noakes CJ, Shorno SA, Williamson RD (2026), "Large-scale chamber
  tests of in-room germicidal ultraviolet (GUV) systems: Review and best
  practices," *Building and Environment* 294: 114357. DOI:
  10.1016/j.buildenv.2026.114357 (open access). Plus its Supplemental
  Information (SI-3, "Governing Equations for Simulation of IAQ Dynamics
  Under Test Conditions"). **The most authoritative and most directly
  relevant reference found in this whole search** — a 2026 field-consensus
  review from an ad-hoc committee (formed at the 2nd International
  Congress on Far UV Science and Technology, 2024) specifically to define
  best practices for chamber testing of GUV systems, including the exact
  steady-state/static-decay/dynamic-decay methodology question this
  project investigates. (Note: co-author Holger Claus, A3Lighting
  Consulting, appears to match this project's own author/consultancy.)

  **The theoretical core (SI-3), worked through in full here**: for a
  well-mixed chamber with challenge-agent source rate S, volume ∀, and
  lumped first-order loss rate `R = AER + k_S + k_E` (ventilation +
  settling + endogenous decay, UV off) or `R_UV = R + k·E'_avg` (UV on,
  k = inactivation constant, E'_avg = spatially-averaged fluence rate):
  - Steady-state: `SS1 = S/(R·∀)` (Eq. SI-6, UV off), `SS2 = S/(R_UV·∀)`
    (Eq. SI-13, UV on).
  - Dynamic decay (from SS1, ventilation still running): UV-off decay
    follows `C(t') = SS1·exp(−R·t')` (Eq. SI-17); UV-on decay follows
    `C(t') = SS1·exp(−R_UV·t')` (Eq. SI-19).
  - **The decay-phase rate constants (R, R_UV) are the exact same
    symbols, defined identically, as the steady-state rate constants that
    set SS1 and SS2.** This isn't an approximation or a coincidence — under
    the well-mixed model, decay-mode and steady-state-mode measure the
    *same* underlying R_UV (and therefore the same eACH_UV = k·E'_avg) by
    mathematical construction, not as independently-varying empirical
    quantities that merely tend to agree.
  - **Direct implication for this project**: since a well-mixed system is
    mathematically *forced* to give matching decay-vs-steady-state eACH_UV,
    a large observed discrepancy (like this project's own ~200%) is
    evidence of a departure from that model — imperfect mixing, a genuine
    difference in the fluence-rate/flow field between how the room is
    driven in each test mode, or a methodological/fitting artifact — not
    evidence that decay and steady-state are simply "different things" for
    an unexplained reason. This is the theoretical grounding for why Miller
    & Macher's real, well-mixed single-lamp case (below) came out within
    ~10%, and for why both papers below single out mixing quality as the
    key variable to check.
  - The main text (Sections 11.4–11.5) independently makes the same
    point qualitatively: local UV reaction rates can never be spatially
    uniform (fluence rate fields have strong gradients by design), so
    strict well-mixedness never holds instantaneously — but *time-averaged*
    measurements over a typical 2–20 min sampling window can still
    approximate the well-mixed model well, since a sample collected over
    that period effectively integrates over the room volume. Section 13
    explicitly lists "definition/standardization of mixing behavior
    characterization" and "a standard method for quantification of UV-C
    disinfection kinetics for aerosolized challenge agents" as open,
    field-wide research gaps — i.e., the uncertainty this project has been
    probing (is Z/k right, does decay match steady-state) is a recognized,
    unsolved problem in the field, not a project-specific shortcoming.
  https://doi.org/10.1016/j.buildenv.2026.114357

- Miller SL, Macher JM (2000), "Evaluation of a Methodology for
  Quantifying the Effect of Room Air Ultraviolet Germicidal Irradiation on
  Airborne Bacteria," *Aerosol Science & Technology* 33(3): 274–295. DOI:
  10.1080/027868200416259. **The closest thing found to a true matched
  decay-vs-steady-state comparison** — read in full (PDF supplied by the
  user, 2026-08-01). Same 36 m³ room, same *B. subtilis* spore aerosol,
  same single unlouvered "lamp A," tested under both protocols
  back-to-back (scenarios BS-s1 steady-state vs. BS-d2 decay, Table 1):
  - **Steady-state** (BS-s1): effectiveness E=56% at 2 ACH ventilation
    (Table 4).
  - **Decay** (BS-d2): ACH_UV = 3.8 h⁻¹ directly fitted (Table 5), against
    a separately-measured non-UV removal rate ACH_V+ACH_O = 2.7 h⁻¹
    (ACH_O = 0.7 h⁻¹ "other" removal — deposition, die-off — also
    decay-derived).
  - **Converting steady-state E to an equivalent ACH_UV for direct
    comparison** (derived here from the paper's own Eq. 2–4 and Table 4/5
    numbers — not a conversion the paper computes itself):
    `ACH_UV = (ACH_V+ACH_O) × E/(1−E) = 2.7 × 0.56/0.44 ≈ 3.4 h⁻¹`.
    That's within ~10% of the decay method's directly-fitted 3.8 h⁻¹ for
    the *same* lamp/species/room — genuine, reasonably close agreement
    under good mixing, not the large divergence this project's own ~200%
    signal might suggest is typical everywhere.
  - **But the same paper documents decay-method fragility directly**: the
    two-lamp decay run (BS-d1, both lamps on) gave ACH_UV = 2.3 h⁻¹ —
    *lower* than the one-lamp run's 3.8, despite more UV power. The
    authors call this "an unexpected result" and attribute it to a poor
    curve fit (R²=0.870 vs. 0.975–0.998 for the other decay runs) from
    noisier, lower-count samples late in that run; fitting only the first
    two points instead gives 6.5 ACH_UV — "approximately twice that
    observed with one lamp," matching physical expectation far better.
  - **The authors' own stated methodological opinion** (Discussion):
    "effectiveness is best used with the steady-state method as it is
    independent of time and mixing affects. Equivalent air-exchange rate
    should be used with the decay method provided mixing is ensured" —
    i.e., decay-derived ACH_UV is the more failure-prone of the two
    metrics, contingent on good mixing and enough well-fit data points,
    not steady-state.
  - Also cites a third, independent methodology for cross-context: Salie
    et al. (1995) single-pass (one-pass-through-the-fixture) bioassay,
    translated to an equivalent steady-state effectiveness of 13%/6%/56%
    for *B. subtilis*/*M. luteus*/*E. coli* in a similarly-sized room —
    a third way this same basic question gets approached in the
    literature.
  https://doi.org/10.1080/027868200416259

- McDevitt JJ, Milton DK, Rudnick SN, First MW (2008), "Inactivation of
  Poxviruses by Upper-Room UVC Light in a Simulated Hospital Room
  Environment," *PLoS ONE* 3(9): e3186. Read in full (PDF supplied by the
  user, 2026-08-01) rather than from search snippets alone, correcting/
  replacing two weaker, partly-misattributed entries this replaced. Ran
  *both* methods, in the same chamber with the same UVC fixtures:
  - **Decay method** (single UVC fixture, 20°C/50%RH; Table 1): eACH_UVC
    = 7 (no fan, no heat boxes) up to 92 (with ceiling-fan mixing) — an
    order-of-magnitude swing driven by mixing state, *within* the decay
    method (not a decay-vs-steady-state comparison by itself, contrary to
    how this was first summarized here from search snippets).
  - **Steady-state method** (1 or 4 fixtures, 2 or 6 ACH, summer/winter;
    Table 2): eACH_UVC ranged 18–1000 across conditions; single-fixture
    conditions specifically ranged 18–150.
  - The paper explicitly flags the methodology question this project is
    asking: "it has been recommended that tests of the efficacy of UVC
    against bioaerosols be based on steady-state measurements rather
    [than] decay experiments" — citing Nicas & Miller (1999), below.
  - **Closest available same-system comparison**: the decay method's
    fan-mixed, single-fixture result (87–92 ACH_UVC) falls within the
    steady-state method's single-fixture range (18–150 ACH_UVC) once
    ACH/season are accounted for — same order of magnitude, not the large
    divergence this project's own ~200% decay-vs-steady-state signal
    might suggest is typical. Caveat: not a controlled matched-condition
    design (decay varied fan/heat-boxes only; steady-state varied
    ACH/fixture-count/season only; temperature/RH protocols also differ
    between the two), so this is suggestive, not a clean answer.
  https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0003186

- Nicas M, Miller SL (1999), "A multi-zone model evaluation of the
  efficacy of upper-room air ultraviolet germicidal irradiation," *Appl
  Occup Environ Hyg* 14: 317–28. The recommendation (steady-state testing
  preferred over decay testing for UVGI efficacy) that *both* Miller &
  Macher (2000, this paper's own second author on the earlier one) and
  McDevitt et al. (2008) cite directly — worth reading first-hand for its
  own reasoning, not yet done in this search. Related: Nicas M (1996),
  "Estimating Exposure Intensity in an Imperfectly Mixed Room," *Am Ind
  Hyg Assoc J* 57: 542–550 — cited by Miller & Macher as the source for
  why poor mixing specifically corrupts decay-method ACH determinations.

- "Estimation of the UV susceptibility of aerosolized SARS-CoV-2 to
  254 nm irradiation using CFD-based room disinfection simulations,"
  *Scientific Reports* (2024) — CFD room-disinfection susceptibility
  fitting, background context for how Z/k is typically extracted from
  simulated room decay curves.
  https://www.nature.com/articles/s41598-024-63472-3

- "Numerical investigation of upper-room UVGI disinfection efficacy in an
  environmental chamber with a ceiling fan," PubMed 23311354 —
  room-scale CFD UVGI study, background context for mixing/eACH
  sensitivity.
  https://pubmed.ncbi.nlm.nih.gov/23311354/

- "A quantitative method for evaluating the germicidal effect of upper
  room UV fields."
  https://www.researchgate.net/publication/38141660_A_quantitative_method_for_evaluating_the_germicidal_effect_of_upper_room_UV_fields

**Overall takeaway** (revised 2026-08-01 after reading Blatchley et al.
2026, Miller & Macher 2000, and McDevitt et al. 2008 in full — see
`ANALYSIS_LOG.md` for full discussion): the question now has a real
theoretical anchor, not just adjacent empirical analogies. Blatchley et
al.'s SI-3 derivation shows that under the well-mixed model, decay-mode
and steady-state-mode eACH_UV are **the same quantity by mathematical
construction** (the same lumped rate constant R_UV governs both SS2 and
the with-UV decay curve) — so a large observed gap between them is, by
that theory, necessarily a signature of departure from well-mixed
conditions (or a methodological/fitting artifact), not evidence that the
two protocols measure genuinely different physical quantities. Miller &
Macher's real, well-controlled single-lamp case empirically confirms this:
decay and steady-state landed within ~10% of each other. The same paper
also shows exactly how that agreement can break (their two-lamp run's
anomalously low decay-derived ACH_UV, attributed to a poor curve fit) and
states outright that decay-derived ACH_UV is reliable only "provided
mixing is ensured," while steady-state effectiveness is "independent of
time and mixing effects." McDevitt et al. (2008) adds a second, less
tightly matched same-chamber comparison landing in the same order of
magnitude. No paper was found that varies *only* protocol while holding
every other condition perfectly fixed on a real physical chamber — that
specific clean experimental test still looks like an open gap — but this
project's own large (~200%) decay-vs-steady-state signal is no longer an
unprecedented result to puzzle over in the abstract: theory says the two
should match under good mixing, so the size of the gap is itself a
measurement of how far this CFD room's mixing (or the two modes' fitting/
methodology) departs from that ideal — exactly the kind of thing this
session's own y+/mesh-grading investigation was probing directly.
