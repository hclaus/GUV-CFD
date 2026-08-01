# GUV-CFD: A Plain-English Guide

This document explains what GUV-CFD is, what it can do, how a simulation
actually works, how to read its results, and what we've learned (the hard
way) about its strengths and limits. It's written for a general reader —
no programming background assumed. A technical appendix at the end lists
the exact simulation settings, for anyone who wants to verify or reproduce
the numbers.

---

## 1. Purpose

Germicidal UV (GUV) lamps disinfect air by damaging pathogens with UV
light. How well that actually works in a real room depends on two things
together: how strong the UV light is at any given point, and how quickly
and thoroughly the air itself moves that pathogen-carrying air through the
UV light.

Most UV-disinfection calculations only model the first half — they assume
the room is "perfectly mixed," meaning every bit of air has an equal,
instant chance of passing through the UV light. Real rooms don't work that
way. Air conditioning, open windows, fans, and the room's own shape create
areas that get swept clean quickly and other areas (corners, behind
furniture, near a door) that barely get any fresh airflow at all. A lamp
that looks powerful on paper can under-perform badly in a room with poor
air circulation, and a modest lamp can outperform expectations in a
well-mixed one.

GUV-CFD closes that gap. It takes a room design (dimensions, lamp
placement and strength) and couples it to a real airflow simulation
(computational fluid dynamics, or CFD) of that same room, including its
actual ventilation and any mixing fan. The result is a physically
realistic answer to "how well will this specific UV setup actually perform
in this specific room, with its specific airflow" — not just "how well
would it perform in an idealized, perfectly-mixed room."

---

## 2. Current capabilities

- Load a room design (dimensions, lamp positions and output) from a
  project file created in a companion design tool.
- Two ways to simulate disinfection: a one-time "clear the room" event
  (**decay mode**), or an ongoing, continuous contamination source that
  the room reaches a steady balance against (**steady-state mode**).
- Configure an air inlet and outlet (position, size, and how the air
  enters — a straight jet, or spread out the way a real ceiling/wall
  diffuser does).
- An optional mixing fan, independent of ventilation.
- An optional "sealed room" setting for decay mode — models a room with no
  mechanical ventilation at all, relying only on a mixing fan (if present)
  to move air.
- Batch runs ("sweeps") over multiple combinations of UV strength and
  ventilation rate in one go, so a whole design-comparison table can be
  produced without babysitting each run individually.
- A live 3D preview of the room, lamps, airflow openings, and fan before
  running anything.
- An in-app results/analysis view for comparing runs.
- A one-click exported Word document report (room setup, a rendered
  preview image, and the full results table) suitable for sharing.
- The ability to open the raw simulation directly in ParaView, a
  professional 3D visualization tool, for an expert to inspect the actual
  airflow and contamination fields in detail.

### 2a. Room setup and the shared UV physics

A project starts as a file created in a separate UV room-design tool - Illuminate, which
captures the room's dimensions, wall/surface properties, and the position,
aiming, and output of every UV lamp. GUV-CFD loads that file and reuses
the exact same underlying UV-physics calculation the design tool itself
uses — how much UV light reaches any given point in the room, accounting
for light reflecting
off walls and surfaces — evaluated at every single point of the airflow
simulation's 3D grid, rather than just a handful of sample points. That
matters because it means the UV-light picture and the airflow picture are
describing the exact same physical room, not two loosely related models.

For the room flow design, the user positions where air enters and
leaves (or seals the room entirely), optionally adds a mixing fan, and
chooses where pathogens should be introduced — either a one-time,
whole-room starting concentration (decay mode) or a specific ongoing
source location (steady-state mode). Moreover, up to 3 monitoring points can be placed in the room to measure pathogen concentration in specific locations.

---

## 3. Simulations

### What is the Euler method?

This app uses the so called Euler method for simulations. Any computer simulation of something that changes over time — like air
moving, or a contaminant clearing out of a room — can't calculate the
entire future at once. Instead, it advances in small steps: given the
current state of everything (how fast the air is moving everywhere, how
much contaminant is present everywhere), it works out what the state will
be a short moment later, then repeats that many, many times to build up
the full picture over time. This "step by step" approach to solving how
something evolves is called the Euler method, one of the oldest and most
fundamental techniques for this kind of problem.

There are two flavors. The simple version ("explicit") predicts the next
moment purely from the current one — fast per step, but it can only take
very small, cautious steps before the calculation becomes numerically
unstable and produces garbage. The version this application uses
throughout ("implicit") instead solves a small system of equations at
each step that accounts for the *next* moment's state too, which costs a
bit more work per step but is far more stable — it can safely take
noticeably bigger time steps without the simulation blowing up, which
matters a lot when a realistic simulation might need to cover many
minutes or hours of real time.

### The basic steps, common to every simulation

1. **Build the room.** A 3D grid ("mesh") is generated directly from the
   room's real dimensions. The grid size can be adjusted under settings. Experience shows that 25 divisions of a room length are a good compromise between simulation time and accuracy.
2. **Carve out the openings.** The air inlet and outlet (or, in a sealed
   room, no openings at all) are cut into that grid at their configured
   positions and sizes.
3. **Work out the required air speed.** Given the target ventilation rate ACH
   (how many times per hour the room's full volume of air should be
   replaced) and the size of the opening, the simulation calculates how
   fast air needs to enter to achieve that.
4. **First simulation step - Let the airflow settle first.** Before anything about contamination
   or UV is introduced, the simulation runs purely on airflow — inlet,
   outlet, and fan (if any) — until that airflow pattern stabilizes into a
   realistic, steady circulation pattern for the room. This step ignores
   contamination and UV entirely; it's purely "what does the air actually
   do in this room."
5. **Introduce contamination and UV, and watch it evolve.** Only once the
   airflow itself is settled does the simulation add the contaminant and
   the UV disinfection effect, and track how the contamination level
   changes over time as it's carried around by that already-solved
   airflow and destroyed wherever UV light reaches it.

This two-stage approach (settle the airflow, *then* solve the
contamination) is both more physically correct and much faster 
computationally than trying to solve everything at once — working out
realistic room airflow is by far the harder and more expensive part of
the calculation; once that's known, tracking a contaminant being carried
along by it and destroyed at a known rate is comparatively simple.

### 3 modes of simulation
When it comes to UV air disinfection 3 testing methods are used. These methods have been described in standards and the literature. They all have advantages and disadvantages. There are well known analytical models that can be used under ideal "well mixed" conditions.

#### Decay mode

The room starts out fully contaminated, uniformly, everywhere. From that
starting point, ventilation, any mixing fan, and UV disinfection  all work together to clear it, and the simulation tracks how the
room's overall contamination level falls over time. A curve is then fitted
to that falling trend to get a single, well-defined "how fast is this
clearing" rate.

In this program, every decay-mode run is actually run twice: once with the UV lamps on,
and once with them switched off entirely (the "control" run, using the
exact same room, airflow, and starting condition). This matters because
ventilation alone also clears contamination — without a UV-off comparison
run, there would be no way to tell how much of the observed clearing was
actually due to the UV lamps versus just the room's own air exchange. The
control run isolates ventilation's own contribution, so the UV-specific
benefit reported afterward is genuinely UV's contribution alone.

#### Steady-state mode

Instead of a one-time contamination event, this mode models an ongoing,
continuous source of contamination (for example, a person who continues
shedding pathogen the whole time they're in the room). Rather than
watching a level fall, the simulation runs until the room's contamination
level stops changing and settles at a constant, ongoing balance point
between "how much contamination is being added" and "how much is being
removed" (by ventilation and, if enabled, UV). This is the so call "steady state" concentration.

This is done in two stages: first with the source running but no UV, to
establish the baseline balance point ventilation alone would reach; after the steady state concentration of this simulation is known, the UV is switched on. This will lead, over time to a different, lower steady state concentration. 
Reaching a genuine, fully-settled balance point (rather than reading a
number too early, while it's still trending) takes real simulated time and
is one of the trickier and more computationally demanding parts of this
mode — see the "problems encountered" section below for how the
reliability of this measurement was improved.

#### Sealed-room mode (decay mode only)

This models a room with no mechanical ventilation whatsoever, a setup which is often used to measure susceptability of pathogens. Every
opening closed off, contamination is well mixed enclosed in the room, typically mixed through an
internal mixing fan, if one is configured, Pathogen reduction is only performed by UV disinfection. I

A completely sealed, motionless room turns out to be a numerically extreme
case for the underlying airflow-simulation software — it doesn't handle
"literally zero air movement everywhere" gracefully by default. Getting
this working reliably was one of the real problems encountered during
development; see item 7 in the next section for the full story.

### What is actually being solved, and when

At its core, every simulation here solves two different physical problems,
one after the other:

1. **How does the air itself move?** This follows the standard equations
   of fluid motion (the same physics behind weather simulation, aircraft
   design, and industrial ventilation design), including a turbulence
   model — real room airflow is turbulent (chaotic, swirling), not smooth,
   and a plain, naive calculation would badly misrepresent that. This is
   solved first, on its own, until it reaches a stable pattern.
2. **How does the contamination move and get destroyed?** Once the
   airflow itself is known and stable, the contaminant is treated as being
   carried along by that airflow (the same way a puff of smoke follows air
   currents) and destroyed at a rate that depends on the local UV light
   intensity — strong where lamps are bright, weak or zero in shadowed
   corners.

---

## 4. Results, analysis, and report writing

### What the headline numbers mean

- **Idealized (perfectly-mixed) UV benefit.** What the UV lamps'
  disinfection rate would be if the room mixed instantly and perfectly —
  computed purely from the product of average UV fluence rate and the suceptability of the pathogen (Z), with no airflow
  simulation involved at all. This is a theoretical upper ceiling of possible UV disinfection, not a
  real-world measurement.
- **Real, simulated UV benefit.** What the UV lamps actually achieve in
  the real, imperfectly-mixed airflow, that is what this simulation solves. This is
  always at or below the idealized figure.
- **Mixing efficiency.** The ratio of the real benefit to the idealized
  ceiling — what fraction of UV's full theoretical potential the room's
  actual airflow allows it to deliver. 100% would mean the room mixes
  essentially perfectly; well below that means real-world circulation is
  meaningfully limiting UV's effectiveness.
- **Measured vs. given ventilation rate.** The ventilation rate the
  user configures is a given for the simulation's air inlet. However, the
  simulation separately measures how much air is actually flowing through
  the room in practice. These two numbers can be different — real
  airflow, even in simulation, doesn't always hit its nominal target
  exactly, for entirely physical reasons (an opening's shape, its
  position relative to the outlet, and so on).
- **Percent reduction.** The overall percentage drop in room contamination
  the whole system (ventilation + UV, or ventilation + fan + UV in a
  sealed room) achieves. This tends to be the most directly interpretable,
  and most reliable, single headline number.

### Two different "how well-mixed is this room" questions

There are two genuinely different ways this application checks how well a
room is mixed, and they don't always agree:

- **Mixing efficiency** (above) asks specifically: does the real airflow
  deliver as much *UV-specific* benefit as the idealized case would? It's
  tied to how long air actually spends near the UV lamps.
- **Spatial uniformity** asks a different, simpler question: at a given
  moment, is the contamination level roughly the same everywhere in the
  room, or are there stagnant pockets and hot spots?

These can point in opposite directions on the very same simulation. In one
real comparison, turning on a stronger mixing fan clearly made the room
*more* spatially uniform (pockets of higher/lower concentration
evened out significantly) — but the UV-specific mixing efficiency actually
got slightly *worse*. The likely explanation: a stronger fan can move air
past the UV lamps faster, evening out the room overall, but giving each
bit of air less time actually exposed to UV light while it's there. Faster
circulation and better UV exposure aren't automatically the same thing —
neither number alone tells the whole story, and both are worth checking.

### Where results show up

- **The in-app Analysis view** — lets you inspect and compare results
  from runs directly inside the application, including the underlying
  curves the numbers were derived from.
- **The exported Word report** — a standalone, shareable document
  containing the room setup, a rendered image of the room/lamp/airflow
  configuration, and the full table of results, suitable for sending to
  someone who doesn't have the application installed.
- **ParaView** — a separate, professional 3D visualization tool the
  application can launch directly into a pre-configured view of a given
  run, showing the actual simulated airflow patterns and contamination
  field in 3D. Intended for an expert user who wants to visually inspect
  the raw simulation rather than just read summary numbers.

---

## 5. Problems encountered, and how they were finally solved

Building a tool that couples a UV-physics calculation to a full airflow
simulation surfaced a number of real, sometimes subtle problems along the
way. Here's the honest history.

**1. A realistic diffuser-style air inlet made the simulation blow up.**
Modeling air entering through a real ceiling/wall diffuser (which spreads
air outward in a pattern, rather than a single straight jet) initially
caused some simulations to diverge outright — numbers growing without
bound until the results were nonsense. The root cause was subtle: right at
the very center of the diffuser opening, the "which direction is air
flowing" calculation was mathematically undefined, and two physically
adjacent points ended up being assigned nearly opposite airflow
directions — a sharp, unrealistic discontinuity that destabilized the
whole calculation. The fix was a smoother, more physically realistic
velocity pattern that blends gradually from straight-into-the-room at the
diffuser's center out to a wide spreading pattern at its edges, the way a
real diffuser actually behaves — solid, not a hole, right at the middle.

**2. Openings and zones sometimes came out oddly shaped.** Depending on
exactly where an opening or contamination zone was positioned relative to
the room's 3D grid, it could come out visibly misshapen — for example, a
ring-shaped opening missing its own center cell — instead of a clean
rectangle. This happened because, for certain grid-alignment coincidences,
the boundary between "include this cell" and "exclude this cell" landed
essentially exactly on a grid line, which the underlying software resolves
almost arbitrarily. The fix: every opening and zone position is now
automatically nudged by a tiny amount (at most half a grid cell) so its
edges land cleanly on the grid, rather than requiring the user to pick
positions and sizes that happen to divide evenly. It should be noted, this limits the size of inlets and outlets to multiples of the grid size and the position as well.

**3. Steady-state results were unreliable if read too early.** Real,
turbulent airflow never fully settles into a perfectly constant number —
it keeps fluctuating even once it's "basically" reached its steady level.
Reading a single final value could be off by a large margin depending on
exactly which moment was read. Through several experiments it was found that the average concentration of pathogens in a room varies significantly less than fluctuation of air flow at specific times and points. This was fixed in two stages: first, by
averaging over a trailing window of time (15%) rather than reading one instant;
later, by mathematically extrapolating that trend to its true long-term
value using a proper curve fit, rather than simply running the simulation
longer and longer to brute-force a tighter average.

**4. A quiet software-default bug significantly understated ventilation's
real benefit.** During an in-depth investigation into why a particular
measurement seemed sensitive to an unrelated setting, it was discovered
that a component responsible for tracking the contamination level's
evolution in steady-state mode had, due to an unset default, effectively
only been taking a single, very coarse step per time interval instead of
properly converging each time interval before moving on. The practical
effect: the measured "how much does ventilation alone remove" figure was
understated by as much as roughly 90% in the worst tested case — meaning
every UV-benefit number derived from it (in every earlier run, not just
new ones) had been systematically biased. It was found through careful,
methodical testing (deliberately varying the suspicious setting and
confirming the bias tracked it exactly), and fixed by explicitly
configuring that component to properly converge at every step, matching
the standard the rest of the simulation was already held to.

**5. Some rooms' airflow genuinely never settles down — it oscillates
forever.** A jet of air striking a wall or floor at an angle, for example,
can produce a stable, repeating back-and-forth pattern that never
converges to one single unchanging state, no matter how long the
simulation runs. Originally, the software would just keep waiting for a
convergence that was mathematically never going to happen. It now
recognizes when a case has settled into a bounded, non-growing oscillation
(rather than genuinely still trending) and accepts that as a valid
end-point, instead of running indefinitely.

**6. Low-ventilation-rate steady-state simulations used to take a very
long time.** A room with little ventilation genuinely takes longer, in
real physical time, to reach a steady balance — but the simulation wasn't
automatically scaling its effort to match. This was substantially sped up
(with no measurable loss of accuracy, confirmed by comparing against much
longer, more expensive reference runs) by taking advantage of a quirk in
how this particular calculation stage works: the airflow part of the
math, in this mode specifically, doesn't actually depend on the passage of
time at all, only the contamination-tracking part does — so that part's
internal "clock" can be sped up independently, letting it cover the same
real elapsed time in far fewer computational steps, without affecting the
airflow accuracy at all. (This particular trick doesn't carry over to
decay mode, whose whole method genuinely depends on tracking real elapsed
time accurately.)

**7. Simulating a fully sealed room (zero ventilation) used to crash
outright.** This surfaced two distinct, related problems. First, the
simulation's optional "quick head start" step (an inexpensive shortcut
used to get airflow roughly into the right shape before the full,
expensive calculation begins) turns out to mathematically require *some*
driving force somewhere in the room — with every opening sealed and truly
zero airflow anywhere, that shortcut step has nothing to work with and
fails outright. Second, and more fundamentally: air pressure, in a fully
enclosed space with no opening to the outside, has no natural absolute
reference point — only *differences* in pressure between two points are
physically meaningful, not any single "this is what pressure equals here"
value. The simulation software refuses to guess a reference point on its
own, and errors out instead. Both problems were fixed directly: the quick
head-start step is now skipped entirely for a sealed room (the full
calculation handles it fine on its own, starting from an appropriately
neutral state instead), and the software is now explicitly told which
single point in the room to treat as its pressure reference.

---

## 6. Likely limitations of the current approach

Being upfront about what this tool doesn't yet fully resolve:

- **Decay mode and steady-state mode currently give substantially
  different answers** — roughly 2 to 3 times apart — for what should
  conceptually be measuring the same underlying UV benefit, in the one
  case tested in detail so far. A serious investigation ruled out one
  leading suspected cause (whether the *type* of underlying calculation
  technique used explains it — it doesn't; both variants agree closely
  with each other and still disagree with decay mode by the same large
  margin), but the true root cause of the gap remains unresolved. Until
  it's resolved, the safer, more directly interpretable number to rely
  on is decay mode's overall percent-reduction figure.
- **Ventilation-delivery findings from one room shouldn't be assumed to
  generalize.** In the one room studied closely, the actual delivered
  airflow consistently ran at only about 44% of its nominal target — a
  large, real gap, but tied to that specific room's opening geometry, not
  a universal correction factor. A different room's mesh could show a very
  different ratio, and that would be expected, not evidence of a bug.
- **The two "how well mixed" measures don't always agree with each
  other** (see the fan example in Section 4) — neither the UV-specific
  mixing efficiency nor the spatial-uniformity number alone fully answers
  "is this room well mixed"; both are worth checking together, and a
  design choice that improves one isn't guaranteed to improve the other.
- **Platform requirement.** The tool currently only runs on Windows with a
  Linux compatibility layer (WSL) and a real installation of the
  underlying open-source CFD engine — there isn't yet a native Mac or
  Linux path.
- **A simulation is still a model, not a measurement.** The airflow
  calculation uses a standard, widely validated turbulence model, not a
  from-scratch physical law — it's a well-established engineering
  approximation, appropriate for design and comparison purposes, but a
  genuinely safety-critical decision should still be informed by
  real-world airflow verification, not simulation results alone.

---

## Appendix (technical details about OpenFoam use)

For a technical reader who wants to verify or reproduce these results, the
production simulation configuration currently in use is:

**Turbulence and fluid properties**
- Turbulence model: `kOmegaSST` (a standard RANS/RAS two-equation
  turbulence model).
- Kinematic viscosity: `nu = 1.5e-5 m^2/s` (air).

**Numerical schemes** (`fvSchemes`)
- Time derivatives (`ddtSchemes`): `Euler` (implicit first-order — see
  Section 3's plain-English explanation).
- Gradients: `Gauss linear` throughout.
- Convection (`divSchemes`):
  - `div(phi,U)`: `bounded Gauss linearUpwindV grad(U)` (2nd-order,
    momentum).
  - `div(phi,k)`, `div(phi,omega)`: `bounded Gauss upwind` (1st-order,
    turbulence quantities).
  - `div(phi,T)`: `bounded Gauss linearUpwind grad(T)` (2nd-order,
    contaminant transport).
- Laplacians: `Gauss linear corrected` throughout.
- Wall distance method: `meshWave`.

**Linear solvers and tolerances** (`fvSolution`)
- Pressure (`p`/`pFinal`): `GAMG`, `GaussSeidel` smoother; tolerance
  `1e-6`, relative tolerance `0.1` (non-final), `0` (final pass).
- Velocity/turbulence/contaminant (`U`, `k`, `omega`, `T` and their
  `*Final` variants): `smoothSolver`, `GaussSeidel`; tolerance `1e-8`,
  relative tolerance `0.1` (non-final), `0` (final pass).
- Flux-correction potential (`Phi`, used only during the optional
  quick-start step): `GAMG`, `GaussSeidel`, tolerance `1e-6`, relative
  tolerance `0.01`.

**Transient solve (`PIMPLE`)** — decay mode and the steady-state
continuous-source runs' transient stages
- `nOuterCorrectors 3`, `nCorrectors 2`, `nNonOrthogonalCorrectors 0`,
  `turbOnFinalIterOnly false`.
- `residualControl`: `p` and `U` each at tolerance `1e-4` (relative
  tolerance `0`).
- Adaptive time-stepping: `adjustTimeStep yes`, `maxCo 5`, `maxDeltaT 5`.
- For a sealed (zero-ventilation) case: an explicit pressure reference
  cell/value (`pRefCell`, `pRefValue`) is added to this block, since no
  boundary in that configuration otherwise fixes an absolute pressure
  level (see Section 5, problem 7).

**Flow-development solve (`SIMPLE`)** — used to settle the airflow field
before contamination/UV is introduced
- `nNonOrthogonalCorrectors 0`, `consistent no` (plain SIMPLE, not
  SIMPLEC).
- `residualControl`: `p`, `U`, and the combined `(k|omega)` field each at
  `1e-4`.
- Same sealed-case pressure-reference addition as `PIMPLE` above, when
  applicable.

**Relaxation factors** (`relaxationFactors`)
- Fields: `p` at `0.3`.
- Equations: `U` and `(k|omega)` at `0.7`; `T` at `0.7`.

**Contaminant transport** (the `scalarTransport1` function object, which
solves the contaminant field `T` — outside and after the main `PIMPLE`
solve each timestep, once per timestep)
- `tolerance 1e-4`, `nCorr 3` (explicitly set so this component actually
  iterates to convergence each step — see Section 5, problem 4, for why
  this specific setting mattered).
- Carries the contaminant source term (a `scalarSemiImplicitSource`, only
  active in steady-state mode) and, if a mixing fan is configured, the
  fan's own `meanVelocityForce` momentum source.
- The UV disinfection effect itself is added as additional sink terms in
  this same fvOptions configuration, split across binned zones by local
  UV inactivation rate.

**Boundary conditions** — assigned per surface type:
- **Inlet**: fixed velocity (`fixedValue`), magnitude derived from the
  target ventilation rate and opening area; fixed turbulence intensity
  (`k`, `omega` both `fixedValue`); contaminant fixed at `0` (fresh air).
- **Outlet**: pressure fixed at `0` (`fixedValue`); velocity and
  turbulence quantities use `inletOutlet` (passively follows the interior
  field on outflow, falls back to a fixed value only if flow reverses
  inward); contaminant `zeroGradient`.
- **Walls** (room surfaces, and — in sealed-room mode — the closed-off
  inlet/outlet openings too): `noSlip` velocity; standard wall-function
  boundary conditions for `k`, `omega`, and `nut` (turbulent viscosity);
  pressure and contaminant both `zeroGradient`.
- **Sealed-room mode specifically**: the inlet/outlet openings are built
  into the 3D mesh itself as genuine wall surfaces (not just a
  zero-velocity inlet) — see Section 5, problem 7, for why a
  zero-velocity-but-still-labeled-inlet configuration wasn't sufficient
  on its own.
