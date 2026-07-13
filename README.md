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
- [ParaView](https://www.paraview.org/) (optional) — only needed for the GUI's "Open in ParaView" button; everything else works without it.

## Getting started

```
git clone https://github.com/hclaus/GUV-CFD.git
cd GUV-CFD
uv sync
uv run python -m guvcfd.app
```
`uv sync` installs everything in `pyproject.toml`, including `guv-calcs` itself (pinned to a specific commit on GitHub). OpenFOAM/WSL and ParaView are separate, manual installs (see Prerequisites) — `uv sync` doesn't touch those.

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
- `guvcfd/ventilation_control.py` — optional UV-off control run (clones the case's mesh/flow field, strips the UV source) to measure the *actual* ventilation-only air-change rate, correcting `mixing_efficiency` for the gap between nominal and achieved ACH.
- `guvcfd/report.py` — `.docx` report export (room setup, rendered preview, results).
- `guvcfd/paraview_launch.py` — launches ParaView with a preset view (volume-rendered T, inlet-seeded streamlines colored by U).
- `guvcfd/app.py` — Dash GUI: load a `.guv` file, configure inlet/outlet/fan and simulation type, preview the case live, run/continue a simulation, and view/export results. Run with `uv run python -m guvcfd.app`.

## Status

The GUI (`guvcfd/app.py`) covers the full workflow: case setup/preview, running a decay or steady-state simulation, extending an already-completed decay run to a longer duration ("Continue"), viewing/comparing results in the Analysis tab, exporting a `.docx` report, and opening the case in ParaView with a working preset view. `guvcfd/cli.py` still exists for scripting/REPL use outside the GUI.
