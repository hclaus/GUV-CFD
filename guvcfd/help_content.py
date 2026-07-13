"""Static markdown content for the GUI's Help menu (About/License/References/
OpenFOAM notes) - kept out of app.py so that file stays about behavior, not
prose.
"""

ABOUT = """
## About GUV-CFD

Couples [guv-calcs](https://github.com/hclaus/guv-calcHC) (germicidal UV
fluence-rate physics) to [OpenFOAM](https://www.openfoam.com/) CFD, so UV
inactivation is modeled as a spatially-varying sink term in real
airflow/ventilation transport, instead of a static room-average calculation.

**What it computes:**
- UV fluence rate directly at OpenFOAM mesh cell centers (occlusion +
  reflectance aware) from a `.guv` project's room/lamp geometry.
- A converged flow field for the room's ventilation setup (inlet/outlet,
  optional mixing fan).
- Either a **decay** scenario (start fully contaminated, watch it clear) or a
  **steady-state** scenario (continuous contaminant source, compare the
  no-UV vs. UV-on equilibrium).
- Three independent eACH (equivalent air changes per hour from UV) estimates
  per run: well-mixed (fluence-based, idealized), CFD-fit (from the actual
  decay/equilibrium curve), and, where derivable without extra cost, a
  measured-ventilation-corrected version of the CFD-fit number.

**Repository:** [github.com/hclaus/GUV-CFD](https://github.com/hclaus/GUV-CFD)

Standalone tool - connected to Illuminate only via the `.guv` project file
format (loaded manually), no shared code.
"""

LICENSE_SUMMARY = """
## License

GUV-CFD is released under the **MIT License** - permissive: you can use,
modify, and redistribute it (including commercially), as long as the
copyright notice is kept. No warranty is provided.

Full text: [`LICENSE`](https://github.com/hclaus/GUV-CFD/blob/main/LICENSE)
in the repository root.

Note this covers GUV-CFD's own code only. It depends on:
- **guv-calcs** (fluence-rate physics) - see that repository for its license.
- **OpenFOAM** - GPL-3.0. GUV-CFD shells out to a separately-installed
  OpenFOAM (via WSL); no OpenFOAM source is included in or distributed with
  this repository.
- **ParaView** (optional, for the "Open in ParaView" button) - separately
  installed, own license (BSD-3-Clause).
"""

REFERENCES = """
## References

*Coming soon - this section is a placeholder.*
"""

OPENFOAM_NOTES = """
## OpenFOAM: what this tool does, and why

### Introduction

This program targets rather unsophisticated, simple room-scale CFD coupled
with GUV disinfection modeling - not state-of-the-art aerosol/droplet
science. It solves for a single continuous contaminant concentration field,
not individual particles.

**Eulerian, not Lagrangian.** There are two fundamentally different ways to
track how contaminant moves through air:

- **Eulerian** (what this tool uses) - the contaminant is represented as a
  *concentration field*: a continuous scalar value at every point of a
  *fixed* mesh, transported by solving an advection-diffusion-reaction
  equation (OpenFOAM's `scalarTransport` function object) directly on that
  grid, the same way temperature or pressure would be solved. This is
  efficient, and it directly gives the volume-averaged, room-scale
  quantities the tool actually reports - decay curves, steady-state
  concentrations, eACH - since those are properties of the *field*, not of
  any individual particle.
- **Lagrangian** (not used here) - individual particles, representing
  discrete respiratory droplets or aerosol parcels of a given size, are
  tracked one at a time as they move through a separately-solved flow
  field, each subject to its own drag, gravity settling, evaporation, and so
  on. This is the natural choice for questions this tool does *not* try to
  answer: where a droplet of a specific size lands, near-field exposure from
  a single cough, deposition on a particular surface.

Practically: this tool cannot tell you what happens to a 5-micron droplet
specifically - only what happens to a well-mixed-ish, room-scale contaminant
concentration field as UV and ventilation remove it over time. That is a
deliberate scope choice, not an oversight - it is the right level of
fidelity for comparing ventilation/UV design choices at the room scale, at a
small fraction of the setup and compute cost a full Lagrangian aerosol
simulation would need.

### The two-stage approach

Every run has two distinct OpenFOAM stages sharing one mesh and one
`controlDict`, but solving genuinely different things:

1. **Flow convergence** (`simpleFoam`, steady-state RANS) - solves for a
   converged velocity/pressure/turbulence field (U, p, k, omega) given the
   inlet/outlet/fan boundary conditions, with the UV-decay scalar transport
   function object (`scalarTransport1`) explicitly **disabled** during this
   stage. Running the scalar transport against a wildly unconverged early
   flow field reliably crashes with a floating-point exception - the scalar
   solver assumes a physically sensible velocity field to advect through.

2. **Transient scalar transport** (`pimpleFoam`) - once the flow field is
   converged, `scalarTransport1` is re-enabled and the UV-decay or
   continuous-source scenario runs as real time-accurate transport of a
   passive scalar `T` (contaminant concentration) through the now-frozen
   flow field, with the UV inactivation/source terms applied as `fvOptions`
   sink/source entries binned into log-spaced `cellZones`.

This split exists because steady flow convergence (SIMPLE-family, cheap) and
genuinely transient scalar transport (PIMPLE-family, needed for a real
decay/build-up curve) have very different computational costs and
solution behavior - there's no reason to pay for full transient flow
resolution when only the flow's *converged* state matters for how the
scalar gets advected.

### Why SIMPLE, not something else, for flow convergence

`simpleFoam` (SIMPLE algorithm) is the standard OpenFOAM choice for
steady-state incompressible RANS on room-ventilation-scale problems - cheap
relative to transient solvers, and a converged steady flow field is exactly
what's needed as pimpleFoam's starting point. Two variants are available:

- **Plain SIMPLE** (default) - under-relaxed, robust, the safe default.
- **Local Time Stepping (LTS)**, via `pimpleFoam` with
  `ddtSchemes.default = localEuler` - each mesh cell gets its own
  pseudo-timestep sized to its local Courant number, which can converge
  faster than uniform-step SIMPLE when different regions of the flow have
  very different length/time scales (e.g. a fast fan jet next to otherwise-
  still air). In practice (tested on a fan-driven case that never fully
  converged under plain SIMPLE), LTS was **not** a reliable fix - it ran
  ~15-20x slower wall-clock per unit of nominal progress on that case, and
  since LTS is still fundamentally a pseudo-steady-state-seeking method, it
  doesn't resolve flows that are genuinely, physically unsteady (below).

A `potentialFoam -writep` inviscid warm start runs before either, to skip
most of the "spin up from a uniform-zero guess" phase - measured to give no
significant speedup on simple cases, but kept since it's cheap and doesn't
hurt.

### Convergence is checked directly, not trusted from OpenFOAM's own residuals

`fvSolution`'s own `SIMPLE{residualControl{}}` (p/U/k/omega thresholds) is
**not** used to decide when to stop - empirically, on these room-ventilation
meshes, residuals plateau around 1e-2/1e-3, well above typical thresholds,
and never trigger an early stop even once the flow field has genuinely
stopped changing physically.

Instead, `simpleFoam` runs in fixed-size chunks (iterations), and after each
chunk the room's volume-averaged pressure is compared against the previous
chunk's value. Once the relative change is ≤0.5%, the flow field is accepted
as converged.

### Genuinely unsteady flows: bounded-oscillation acceptance

Some flows - most notably a fan or inlet jet impinging directly on a wall or
floor - never satisfy that 0.5% threshold no matter how long `simpleFoam`
runs. This was diagnosed on a real case: 11,500+ iterations still showed
persistent, non-damping oscillation in volume-averaged pressure, with
chunk-to-chunk swings as large as +453%. This is genuine physical flow
instability (an unsteady impinging jet), not a numerical tuning problem - no
steady-state-seeking method (SIMPLE, SIMPLEC, or LTS) can be expected to
"converge" a flow that is actually unsteady.

So if the flow field never converges but has settled into a *bounded*
oscillation (the swing over the most recent several chunks isn't still
growing or drifting relative to the swing before that), it's accepted as-is
rather than raising an error. This was empirically verified before being
relied on: two flow-field snapshots frozen 500 iterations apart during
exactly this kind of oscillation produced downstream eACH_uv results within
~2% of each other - which point in the oscillation cycle the flow field
happens to be frozen at doesn't meaningfully affect the scalar-transport
result that's actually the tool's output of interest.

### Turbulence model

`kOmegaSST` RANS closure throughout. A full scale-resolving approach (LES or
DNS) would capture the impinging-jet unsteadiness above directly rather than
needing the acceptance criterion, but at a compute cost far beyond what's
practical for interactive room-ventilation case exploration - RANS is the
standard, appropriate choice at this scale and level of engineering fidelity.

### Known problems / failure modes to watch for

- **Impinging jets/fans directly facing a wall or floor** may never
  numerically converge (see above) - this is physical, not a bug. If the
  bounded-oscillation check also fails (amplitude still growing), the mesh,
  boundary condition placement, or `max_iterations` genuinely need
  attention.
- **OpenFOAM's `fileName` class rejects spaces** in case directory paths -
  internal utilities (e.g. `postProcess`) can fail with
  `fileName::stripInvalid()` errors if a case (or a subfolder GUV-CFD
  creates, like a control run) has a space in its name.
- **WSL path/launch quirks**: `wsl.exe` occasionally fails to launch with no
  captured output at all (not a real command failure - retried
  automatically); shell commands built from Windows-side paths need careful
  quoting since WSL/Windows path conventions differ (spaces, backslash vs.
  forward slash).
- **Single-block room mesh**: the mesh generator produces one `blockMesh`
  block with `topoSet`-carved inlet/outlet openings, sized directly from the
  room's bounding box - it does not support non-box room geometry or
  internal obstructions/furniture.
- **`postProcess` time-range gotchas**: it processes whatever time
  directories are physically present on disk, largely independent of
  `controlDict`'s own `endTime` setting - if stray time directories from an
  unrelated run exist in a case folder, `postProcess` can silently merge
  them in. Similarly, `postProcess` and `paraview.simple`'s reader cache
  timesteps as of when they were invoked/opened - they don't automatically
  notice new time directories written afterward (e.g. by a "Continue" run
  that extends an already-open case) without an explicit refresh/reload.
"""
