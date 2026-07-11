# GUV-CFD

Couples [guv-calcs](https://github.com/hclaus/guv-calcHC) (germicidal UV fluence-rate physics) to [OpenFOAM](https://www.openfoam.com/) CFD, so UV inactivation can be modeled as a spatially-varying sink term in real airflow/ventilation transport, instead of a static room-average calculation.

Standalone tool, connected to [Illuminate](https://github.com/hclaus/Illuminate_polygon) only via the `.guv` project file format (loaded manually) — no shared code or git relationship. Both projects depend on the same underlying `guv-calcs` physics library.

## What it does

- Computes UV fluence rate directly at OpenFOAM mesh cell centers (occlusion + reflectance aware, no CSV/interpolation round-trip) from a `.guv` project's room/lamp geometry.
- Generates a room mesh (single-block, `topoSet`-carved inlet/outlet) sized directly from the `.guv` room's dimensions.
- Runs the full flow-convergence → UV-decay or steady-state-source pipeline, including all the OpenFOAM-specific bookkeeping this needed (`fvOptions` splicing into `controlDict`'s `scalarTransport` function object, `SIMPLE`/`PIMPLE` `fvSolution` coexistence, `writeInterval` sync across nested function objects, `mapFields` warm-starting from a converged reference case).
- Optional continuous contaminant source (steady-state build-up/mitigation scenarios) and mixing fan (`meanVelocityForce`).
- Computes and compares three independent eACH (equivalent air changes per hour from UV) estimates: well-mixed (fluence-based), decay-curve-fit, and steady-state-ratio.

## Prerequisites

- Python 3.11+, [uv](https://docs.astral.sh/uv/)
- WSL with OpenFOAM installed (developed against OpenFOAM v2412 on Ubuntu/WSL2) — this tool shells out to `wsl.exe` to run `blockMesh`/`topoSet`/`simpleFoam`/`pimpleFoam`/`postProcess`, so it currently only works from Windows with WSL. Native Linux support would just need `wsl_utils.py`'s subprocess calls adjusted to run directly instead of through `wsl -e bash -lc`.

## Structure

- `guvcfd/case_io.py` — read/write OpenFOAM field files, boundary patch names.
- `guvcfd/fluence.py` — fluence rate + UV inactivation rate + well-mixed eACH, computed directly at arbitrary points via `guv_calcs`' internal `LightingCalculator`.
- `guvcfd/mesh_gen.py` — `blockMeshDict`/`topoSetDict`/`createPatchDict`/`mapFieldsDict` generation from room dimensions.
- `guvcfd/initial_fields.py` — `0/{U,p,k,omega,nut,T}` generation, ACH-derived inlet velocity.
- `guvcfd/cellzones.py` — bins the continuous UV inactivation rate into log-spaced `cellZones` + `fvOptions` sink terms.
- `guvcfd/contaminant_source.py` — continuous contaminant source (steady-state scenarios).
- `guvcfd/fan.py` — optional mixing fan (`meanVelocityForce`).
- `guvcfd/monitoring.py` — volume-average + patch-average field tracking.
- `guvcfd/splice.py` — `controlDict`/`fvSolution` surgery (fvOptions splicing, function-object enable/disable, time-parameter sync).
- `guvcfd/decay_analysis.py` — decay-curve fitting, convergence/plateau detection, results summary.
- `guvcfd/run_pipeline.py` — `setup_case()`, the one-call orchestrator for mesh → flow convergence → fluence/UV-zones → splice.
- `guvcfd/steady_state_pipeline.py` — `run_steady_state_scenario()`, the two-phase (no-UV steady state → UV-on steady state) orchestrator.
- `guvcfd/visualization.py` — 3D case preview (room, lamps, inlet/outlet, fan) built on `guv_calcs`' `RoomPlotter`.
- `guvcfd/app.py` — Dash GUI: load a `.guv` file, configure inlet/outlet/fan and simulation type, preview the case live. Run with `uv run python -m guvcfd.app`.

## Status

Early — case setup/preview GUI exists (`guvcfd/app.py`); running a simulation and viewing results is still only wired up via a Python REPL / `guvcfd/cli.py`, not yet from the GUI.
