"""Run the full OpenFOAM case setup pipeline end-to-end from a single call:
mesh generation, OpenFOAM binary invocations (blockMesh/topoSet/createPatch/
checkMesh/writeCellCentres/mapFields), fluence/k computation, cellZones/
fvOptions binning, and fvOptions splicing - given just a .guv project and a
target case directory. Everything else this package's modules do piecewise
via manual WSL shell-outs, this ties into one command.
"""
import shutil
from pathlib import Path

from guv_calcs import Project

from .case_io import read_cell_centers, read_boundary_patch_names, write_scalar_field
from .cellzones import bin_decay_rates, write_cellzones, write_fvoptions
from .contaminant_source import write_fvoptions_file
from .decay_analysis import read_vol_average_dat
from .fan import write_fan_topo_set_dict, fan_fvoptions_entry
from .fluence import compute_fluence_at_points, compute_inactivation_rate, compute_well_mixed_eACH
from .initial_fields import (
    write_initial_fields, compute_inlet_velocity, restore_boundary_conditions, resolve_inlet_velocity,
)
from .mesh_gen import write_mesh_dicts, write_map_fields_dict, opening_center, opening_half_extents
from .monitoring import write_vol_average_dict
from .splice import (
    splice_fv_options_into_control_dict,
    set_function_object_enabled,
    set_control_dict_time,
    ensure_simple_fvsolution,
    set_lts_ddt_scheme,
    set_relaxation_factors,
)
from .wsl_utils import (
    wsl_path as _wsl_path,
    run_wsl as _run_wsl,
    run_wsl_or_raise as _run_wsl_or_raise,
    run_wsl_streaming as _run_wsl_streaming,
    StoppedByUser,
)


def _is_stable_oscillation(history, window, growth_tol):
    """True if the last `window` chunk values oscillate within a range that
    isn't growing (or drifting) compared to the `window` chunks before that -
    i.e. genuinely bounded turbulent unsteadiness (an impinging jet/fan hitting
    a wall never settles to a single value, but keeps cycling through roughly
    the same range) rather than a still-settling or diverging flow field.
    Needs at least 2*window chunks of history to make the comparison
    meaningful; returns False otherwise (safe default - keep the hard failure
    when there isn't enough evidence either way).
    """
    if len(history) < 2 * window:
        return False
    older, newer = history[-2 * window:-window], history[-window:]
    old_amp, new_amp = max(older) - min(older), max(newer) - min(newer)
    if old_amp == 0 and new_amp == 0:
        return True
    if new_amp > growth_tol * old_amp:
        return False
    drift = abs(sum(newer) / len(newer) - sum(older) / len(older))
    return drift <= max(new_amp, old_amp)


def converge_flow_field(case_dir, n_iterations=500, fan_entry=None, log_fn=print,
                         max_iterations=20000, check_field="p", rel_tol=0.01, should_stop=None,
                         method="simple", oscillation_window=6, oscillation_growth_tol=1.5,
                         solver_log_fn=None):
    """Run simpleFoam to actually converge the flow field on this mesh,
    starting from whatever is in 0/ (e.g. a mapFields warm start), then copy
    the result back into 0/ so it becomes pimpleFoam's starting point.

    mapFields alone only gives an interpolated *initial guess* - it doesn't
    verify or produce a converged solution for this mesh's specific topology.
    scalarTransport1 is temporarily disabled (see
    splice.set_function_object_enabled's docstring - running the UV-decay
    scalar transport against a wildly unconverged early flow field crashes
    with a floating-point exception), and simpleFoam gets its own iteration
    budget via controlDict's endTime/deltaT/writeInterval, separate from
    whatever pimpleFoam's transient duration is set to (iterations and
    physical seconds are different things, even though both solvers read
    the same controlDict).

    Any solver (not just via the scalarTransport function object) also
    auto-loads constant/fvOptions directly if it exists - if this is called
    before fluence/cellZones/fvOptions have been (re)generated for the
    current mesh, a stale fvOptions from a previous run on a *different*
    mesh will reference cellZones that don't exist yet, and simpleFoam fails
    immediately. constant/fvOptions is normally meaningless during flow
    development (the UV/source sink terms target "T", which simpleFoam
    doesn't even solve), so it's removed here rather than reordering the
    whole pipeline - *except* fan_entry (see fan.py's meanVelocityForce
    entry), which acts on U directly and so is relevant during flow
    convergence too: a real fan affects the converged flow field itself,
    not just the later scalar-transport phases.

    Convergence is checked directly rather than trusted from fvSolution's
    own SIMPLE{residualControl{}} (p/U/k/omega at 1e-4): empirically, on
    these room-ventilation meshes, residuals plateau around 1e-2/1e-3 - well
    above that threshold - and never trigger simpleFoam's own early stop,
    even once the flow field itself has stopped changing physically. So
    instead this runs in n_iterations-sized chunks, and after each chunk
    compares the room's volume-averaged `check_field` (a representative
    scalar flow quantity - p by default) against the previous chunk's value;
    once the relative change is <= rel_tol, the flow field is accepted as
    converged. Capped at max_iterations total to avoid a runaway on a case
    that never plateaus.

    If max_iterations is reached without converging, this doesn't
    unconditionally raise: some flows (e.g. a fan jet impinging directly on a
    wall/floor) are genuinely, persistently unsteady and will never satisfy
    rel_tol no matter how long simpleFoam runs - that's real turbulence, not
    a numerical tuning problem. So the last 2*oscillation_window chunks are
    checked for *bounded* oscillation (see _is_stable_oscillation): if the
    swing in volAverage(check_field) over the most recent oscillation_window
    chunks isn't growing (nor drifting) relative to the oscillation_window
    chunks before that, the field is accepted as-is rather than raising.
    This was verified empirically (not just assumed): two flow-field
    snapshots frozen 500 iterations apart during exactly this kind of bounded
    oscillation produced eACH_uv_effective within ~2% of each other, so which
    point in the cycle the field gets frozen at doesn't meaningfully affect
    the downstream scalar-decay result. Only raises if the field is still
    trending/growing (a real non-convergence problem) or if there isn't
    enough chunk history yet to tell the two cases apart.

    method: "simple" (default) runs simpleFoam under plain SIMPLE/SIMPLEC.
    "lts" runs pimpleFoam under Local Time Stepping (ddtSchemes.default =
    localEuler, see splice.set_lts_ddt_scheme) - each cell gets its own
    pseudo-timestep sized to its own local Courant number, which can
    converge much faster than uniform-step SIMPLE for flows with very
    different length/time scales in different regions (e.g. a fast fan jet
    next to otherwise-still air). The ddtScheme is always restored back to
    Euler before returning (success, failure, or stop) - the later
    transient (real time-accurate) pimpleFoam decay run needs that, not LTS.

    solver_log_fn: on_line callback for simpleFoam/pimpleFoam's raw stdout
    (a few lines per iteration, thousands of lines over a full convergence
    run) - defaults to log_fn if not given, but callers with a visible run
    log generally want a quieter callback here so per-iteration solver
    chatter doesn't flood it (log_fn's own chunk-boundary narration is
    unaffected either way).
    """
    case_dir_wsl = _wsl_path(case_dir)
    solver = "pimpleFoam" if method == "lts" else "simpleFoam"

    log_fn(f"Flow-convergence budget: {max_iterations} iterations max, in chunks of {n_iterations}...")

    log_fn("Disabling scalarTransport1 for flow development...")
    set_function_object_enabled(case_dir, "scalarTransport1", False)

    if fan_entry is not None:
        log_fn("Writing fan-only constant/fvOptions (kept active during flow "
               "convergence, unlike the UV/source entries)...")
        write_fvoptions_file(case_dir, [fan_entry])
    else:
        log_fn("Removing any stale constant/fvOptions (solvers auto-load it if present, "
               "and it's meaningless during flow-only development with no fan)...")
        _run_wsl("rm -f constant/fvOptions", case_dir_wsl)

    log_fn("Ensuring fvSolution has a SIMPLE{} block with under-relaxation "
           "(the reference case's fvSolution was only ever set up for PIMPLE)...")
    ensure_simple_fvsolution(case_dir)

    if method == "lts":
        log_fn("Switching to Local Time Stepping (ddtSchemes.default = localEuler) "
               "for pseudo-transient flow convergence via pimpleFoam...")
        set_lts_ddt_scheme(case_dir, True)

    log_fn("Running potentialFoam for a better initial guess than uniform-zero "
           "(cheap inviscid/irrotational solve, skips most of the 'spin up from "
           "nothing' phase simpleFoam would otherwise need)...")
    r = _run_wsl("potentialFoam -writep", case_dir_wsl)
    if r.returncode != 0:
        log_fn(f"  potentialFoam failed (exit {r.returncode}) - continuing from the "
                f"uniform-zero initial guess instead, this is an optimization, not "
                f"a requirement:\n{(r.stdout + r.stderr)[-1500:]}")
    else:
        log_fn("  potentialFoam initial guess written.")

    log_fn(f"Writing flow-convergence monitor (room volume-average {check_field})...")
    write_vol_average_dict(case_dir, field=check_field, patches=())

    prev_avg = None
    total_run = 0
    converged = False
    history = []

    try:
        while total_run < max_iterations:
            chunk_end = total_run + n_iterations
            log_fn(f"Running {solver} iterations {total_run + 1}-{chunk_end} "
                   f"(chunk size {n_iterations})...")
            set_control_dict_time(case_dir, end_time=n_iterations, write_interval=n_iterations, delta_t=1)

            r = _run_wsl_streaming(
                f"{solver} 2>&1 | tee log.{solver}", case_dir_wsl,
                on_line=solver_log_fn or log_fn, should_stop=should_stop, kill_pattern=solver,
            )
            if should_stop is not None and should_stop():
                raise StoppedByUser("Stopped during flow convergence.")
            if r.returncode != 0 or "FOAM FATAL" in r.stdout or "Floating Point Exception" in r.stdout:
                tail = "\n".join(r.stdout.splitlines()[-25:]) or "(no output captured)"
                raise RuntimeError(f"{solver} failed (exit {r.returncode}):\n{tail}")

            r = _run_wsl_or_raise(
                "ls -d [0-9]*/ 2>/dev/null | sed 's#/##' | sort -n | tail -1",
                case_dir_wsl, "listing time directories",
            )
            latest = r.stdout.strip()
            if not latest or latest == "0":
                raise RuntimeError(f"{solver} did not write any new time directory (found: {latest!r})")
            total_run = chunk_end

            _run_wsl_or_raise("rm -rf postProcessing", case_dir_wsl, "clearing stale postProcessing")
            _run_wsl_or_raise("postProcess -dict system/volAverageDict", case_dir_wsl, "postProcess flow monitor")
            _, vals = read_vol_average_dat(f"{case_dir}/postProcessing/volAverage1/0/volFieldValue.dat")
            cur_avg = vals[-1]

            if prev_avg is not None and prev_avg != 0:
                rel_change = abs(cur_avg - prev_avg) / abs(prev_avg)
                log_fn(f"  [{total_run} iterations total] volAverage({check_field}) = {cur_avg:.6g} "
                       f"(change since last chunk: {rel_change * 100:.3f}%, target <={rel_tol * 100:.2g}%)")
                if rel_change <= rel_tol:
                    converged = True
            else:
                log_fn(f"  [{total_run} iterations total] volAverage({check_field}) = {cur_avg:.6g} (first chunk)")
            prev_avg = cur_avg
            history.append(cur_avg)

            log_fn(f"  Copying fields from {latest}/ to 0/ (excluding T - that's our fresh UV-decay "
                   f"starting condition, not a flow quantity) so the next chunk continues from here...")
            r = _run_wsl_or_raise(f"ls {latest}/ | grep -v '^uniform$' | grep -v '^T$'",
                                   case_dir_wsl, "listing converged field files")
            field_files = r.stdout.split()
            cp_targets = " ".join(f"{latest}/{f}" for f in field_files)
            _run_wsl_or_raise(f"cp -f {cp_targets} 0/", case_dir_wsl, "copying converged fields")
            _run_wsl_or_raise(
                "for d in [0-9]*/; do [ \"$d\" = \"0/\" ] || rm -rf \"$d\"; done",
                case_dir_wsl, "cleaning time directories",
            )

            if converged:
                break

        accepted_oscillation = False
        if not converged:
            accepted_oscillation = _is_stable_oscillation(history, oscillation_window, oscillation_growth_tol)
            if not accepted_oscillation:
                raise RuntimeError(
                    f"Flow field did not converge within {max_iterations} iterations "
                    f"(volAverage({check_field}) still changing more than {rel_tol * 100:.2g}% "
                    f"per {n_iterations}-iteration chunk, and not settling into a bounded "
                    f"oscillation either) - the mesh/BCs may need attention, or max_iterations "
                    f"needs raising for this case."
                )
    finally:
        if method == "lts":
            log_fn("Restoring ddtSchemes.default = Euler (the later transient pimpleFoam "
                   "decay run needs real time-accurate stepping, not LTS)...")
            set_lts_ddt_scheme(case_dir, False)

    if converged:
        log_fn(f"Flow field converged after {total_run} iterations total "
               f"(volAverage({check_field}) changed <={rel_tol * 100:.2g}% in the last chunk).")
    else:
        log_fn(f"Flow field did not fully converge within {total_run} iterations, but "
               f"volAverage({check_field}) has settled into a bounded oscillation (not still "
               f"growing or drifting) rather than genuinely diverging - accepting it as-is. "
               f"This is expected for flows with a jet/fan impinging directly on a wall or "
               f"floor (real unsteady turbulence, not a numerical convergence problem); "
               f"verified empirically that the downstream T-decay/eACH_uv result is "
               f"insensitive to exactly which point in the oscillation the field is frozen at.")

    log_fn("Re-enabling scalarTransport1 for the transient UV-decay run...")
    set_function_object_enabled(case_dir, "scalarTransport1", True)

    log_fn(f"Restoring system/volAverageDict to track T (was tracking {check_field} "
           f"for flow convergence) - the decay-analysis step downstream needs it.")
    write_vol_average_dict(case_dir)
    log_fn("  Clearing postProcessing/ from the p-tracking runs above - otherwise OpenFOAM "
           "detects the changed field name and versions the T output into volFieldValue_0.dat "
           "instead of the plain volFieldValue.dat that decay_analysis reads, silently leaving "
           "the stale p data in place under the expected filename.")
    _run_wsl("rm -rf postProcessing", case_dir_wsl)

    return str(total_run)


def setup_case(guv_path, case_dir, template_case_dir=None, cell_size=0.1, Z=2.0, nbins=25,
               source_field="T", map_from_case=None, map_from_time=0, ach=3.0,
               inlet_wall="xMin", inlet_center=(0.5, 0.85), inlet_size=(0.3, 0.3),
               inlet_diffuser_type="direct",
               outlet_wall="xMax", outlet_center=(0.5, 0.15), outlet_size=(0.3, 0.3),
               inlet2_wall=None, inlet2_center=None, inlet2_size=None,
               inlet2_diffuser_type="direct",
               outlet2_wall=None, outlet2_center=None, outlet2_size=None,
               converge_flow=True, simple_foam_iterations=500, flow_convergence_method="simple",
               flow_rel_tol=0.01, flow_max_iterations=20000,
               momentum_relaxation=None, scalar_relaxation=None,
               pimple_end_time=120, pimple_write_interval=10, pimple_delta_t=0.5,
               fan_speed=None, fan_center=None, fan_direction=(0, 0, -1),
               fan_disk_radius=0.6, fan_disk_thickness=0.2, fan_height=None,
               log_fn=print, should_stop=None, solver_log_fn=None):
    """Set up an OpenFOAM case end-to-end from a .guv project. Returns a dict
    summarizing the run (room dims, lamp count, fluence/k ranges, zone count).

    ach: target air changes per hour [1/hr] - inlet velocity is derived from
    this and the room volume/inlet area (see initial_fields.compute_inlet_velocity),
    rather than a fixed velocity that would silently drift ACH as room size changes.

    inlet_diffuser_type/inlet2_diffuser_type: "direct" (a single vector
    straight into the room, matching every version of this pipeline before
    this parameter existed) or "ceiling" (a real diffuser's discharge:
    velocity spread radially outward across the opening, in the plane of
    its wall - see initial_fields.resolve_inlet_velocity/
    compute_radial_inlet_velocities for the physical justification).

    inlet2_wall/center/size, outlet2_wall/center/size: an optional 2nd inlet
    and/or 2nd outlet, each on any of the 6 room walls. When given, both
    inlets share the same velocity magnitude (see
    initial_fields.compute_inlet_velocities) - flow splits between them in
    proportion to each inlet's own opening area. None (the default) means
    "no 2nd opening", matching today's single-inlet/outlet behavior exactly.

    converge_flow: if True, run simpleFoam (see converge_flow_field()) to get
    a genuinely converged flow field for this mesh, rather than trusting
    mapFields' interpolated guess as-is.

    flow_max_iterations: hard cap on total simpleFoam/pimpleFoam iterations
    during flow convergence (see converge_flow_field's own docstring for
    what happens when this is hit without converging) - GUI-exposed as a
    cross-project "advanced" default (Settings menu, right of File), like
    flow_rel_tol/cell_size/nbins above.

    momentum_relaxation/scalar_relaxation: SIMPLE under-relaxation factors
    for U/(k|omega) and T respectively (see splice.set_relaxation_factors)
    - None (the default) leaves the template's own values untouched.
    GUI-exposed as cross-project "advanced" defaults too.

    pimple_end_time/pimple_write_interval: the transient UV-decay run's
    simulated duration [s] and write cadence [s] - GUI-exposed per-project
    (Project Setup tab), like Z and ach above.

    pimple_delta_t/flow_rel_tol/cell_size/nbins: GUI-exposed too, but as
    cross-project "advanced" defaults (Settings menu, right of File) rather
    than per-project fields - see app_settings.py.

    fan_speed: if given (m/s, see fan.SPEED_RANGE), adds an optional mixing
    fan (see fan.py) - a cylindrical cellZone (default radius 0.6m, a 1.2m
    diameter fan) with a meanVelocityForce driving that zone's mean velocity
    to fan_speed in fan_direction (default straight down, like a ceiling
    fan). Stays active through flow convergence *and* the pimpleFoam phase
    (a real fan affects the whole scenario, not just part of it) - unlike
    the UV/source entries, which only apply once scalar transport starts.
    fan_center defaults to room center in x/y, 30cm below the ceiling, if
    not given.
    """
    case_dir_wsl = _wsl_path(case_dir)
    summary = {}

    Path(case_dir).mkdir(parents=True, exist_ok=True)
    Path(f"{case_dir}/case.foam").touch()
    log_fn("Touched case.foam (ParaView marker file) - present from the start so it's "
           "there regardless of whether this run finishes, fails, or gets interrupted.")

    if template_case_dir is not None:
        log_fn(f"Copying static config from {template_case_dir} ...")
        Path(f"{case_dir}/system").mkdir(parents=True, exist_ok=True)
        Path(f"{case_dir}/constant").mkdir(parents=True, exist_ok=True)
        for name in ("controlDict", "fvSchemes", "fvSolution", "volAverageDict"):
            src = Path(f"{template_case_dir}/system/{name}")
            if src.exists():
                shutil.copy(src, f"{case_dir}/system/{name}")
        for name in ("transportProperties", "turbulenceProperties"):
            src = Path(f"{template_case_dir}/constant/{name}")
            if src.exists():
                shutil.copy(src, f"{case_dir}/constant/{name}")
        if momentum_relaxation is not None or scalar_relaxation is not None:
            set_relaxation_factors(case_dir, momentum_factor=momentum_relaxation,
                                    scalar_factor=scalar_relaxation)

    log_fn(f"Loading project {guv_path} ...")
    project = Project.load(guv_path)
    room = next(iter(project.rooms.values()))
    log_fn(f"  Room {room.x}x{room.y}x{room.z} {room.units}, {len(room.lamps)} lamp(s)")
    summary["room"] = (room.x, room.y, room.z, str(room.units))
    summary["n_lamps"] = len(room.lamps)

    log_fn("Writing mesh dicts (blockMeshDict/topoSetDict/createPatchDict)...")
    write_mesh_dicts(case_dir, room.x, room.y, room.z, cell_size=cell_size,
                      inlet_wall=inlet_wall, inlet_center=inlet_center, inlet_size=inlet_size,
                      outlet_wall=outlet_wall, outlet_center=outlet_center, outlet_size=outlet_size,
                      inlet2_wall=inlet2_wall, inlet2_center=inlet2_center, inlet2_size=inlet2_size,
                      outlet2_wall=outlet2_wall, outlet2_center=outlet2_center, outlet2_size=outlet2_size)

    log_fn("Running blockMesh...")
    _run_wsl_or_raise("blockMesh", case_dir_wsl, "blockMesh")

    log_fn("Running topoSet...")
    _run_wsl_or_raise("topoSet", case_dir_wsl, "topoSet")

    log_fn("Running createPatch -overwrite...")
    _run_wsl_or_raise("createPatch -overwrite", case_dir_wsl, "createPatch")

    log_fn("Running checkMesh...")
    r = _run_wsl_or_raise("checkMesh", case_dir_wsl, "checkMesh")
    if "Mesh OK" not in r.stdout:
        raise RuntimeError(f"checkMesh did not report Mesh OK:\n{r.stdout}")
    log_fn("  Mesh OK")

    fan_entry = None
    if fan_speed is not None:
        center = fan_center or (room.x / 2, room.y / 2, (fan_height if fan_height is not None else room.z - 0.3))
        p1 = (center[0], center[1], center[2] - fan_disk_thickness / 2)
        p2 = (center[0], center[1], center[2] + fan_disk_thickness / 2)
        log_fn(f"Carving fan cellZone at {center}, radius={fan_disk_radius}, speed={fan_speed} m/s...")
        write_fan_topo_set_dict(case_dir, p1, p2, fan_disk_radius)
        _run_wsl_or_raise("topoSet -dict system/fanTopoSetDict", case_dir_wsl, "topoSet (fan zone)")
        fan_entry = fan_fvoptions_entry(fan_speed, direction=fan_direction)
        summary["fan"] = {"center": center, "speed": fan_speed, "direction": fan_direction}

    room_volume = room.x * room.y * room.z
    openings = [(inlet_wall, inlet_size[0] * inlet_size[1])]
    if inlet2_wall is not None:
        openings.append((inlet2_wall, inlet2_size[0] * inlet2_size[1]))
    total_area = sum(a for _, a in openings)
    v_mag = compute_inlet_velocity(ach, room_volume, total_area)

    # Mesh already exists at this point (blockMesh/topoSet/createPatch
    # above) - resolve_inlet_velocity() can read the "ceiling" diffuser's
    # real per-face geometry straight from constant/polyMesh, no need to
    # wait for the writeCellCentres step further below. Computed once,
    # reused for every write_initial_fields()/restore_boundary_conditions()
    # call in this function - stateless/cheap, and mesh geometry doesn't
    # change mid-run.
    inlet_velocity = resolve_inlet_velocity(
        case_dir, "inlet", inlet_wall,
        opening_center(inlet_wall, room.x, room.y, room.z, inlet_center, inlet_size, cell_size=cell_size),
        v_mag, diffuser_type=inlet_diffuser_type,
        half_extents=opening_half_extents(inlet_wall, room.x, room.y, room.z, inlet_center, inlet_size,
                                           cell_size=cell_size))
    inlet2_velocity = None
    if inlet2_wall is not None:
        inlet2_velocity = resolve_inlet_velocity(
            case_dir, "inlet2", inlet2_wall,
            opening_center(inlet2_wall, room.x, room.y, room.z, inlet2_center, inlet2_size, cell_size=cell_size),
            v_mag, diffuser_type=inlet2_diffuser_type,
            half_extents=opening_half_extents(inlet2_wall, room.x, room.y, room.z, inlet2_center, inlet2_size,
                                               cell_size=cell_size))
    log_fn(f"Writing initial fields (0/{{U,p,k,omega,nut,T}}), ACH={ach} -> "
           f"inlet velocity magnitude {v_mag:.4g} m/s ({inlet_diffuser_type})"
           + (f", inlet2 ({inlet2_diffuser_type})" if inlet2_velocity else "")
           + f" (room volume={room_volume:.3g} m^3, total inlet area="
           f"{total_area:.3g} m^2)...")
    has_outlet2 = outlet2_wall is not None
    Path(f"{case_dir}/0").mkdir(parents=True, exist_ok=True)
    write_initial_fields(case_dir, inlet_velocity=inlet_velocity, inlet2_velocity=inlet2_velocity,
                          has_outlet2=has_outlet2)
    summary["ach"] = ach
    summary["inlet_velocity"] = inlet_velocity
    if inlet2_velocity:
        summary["inlet2_velocity"] = inlet2_velocity

    log_fn("Running writeCellCentres...")
    _run_wsl_or_raise("postProcess -func writeCellCentres -time 0", case_dir_wsl, "writeCellCentres")

    if map_from_case is not None:
        log_fn("Writing mapFieldsDict...")
        patch_names = read_boundary_patch_names(case_dir)
        write_map_fields_dict(case_dir, patch_names)
        log_fn(f"Running mapFields from {map_from_case} ...")
        map_from_wsl = _wsl_path(map_from_case)
        _run_wsl_or_raise(f"mapFields {map_from_wsl} -sourceTime {map_from_time}", case_dir_wsl, "mapFields")
        log_fn("  mapFields done; regenerating true cell centers (mapFields overwrites Cx/Cy/Cz too)...")
        _run_wsl_or_raise("postProcess -func writeCellCentres -time 0", case_dir_wsl, "writeCellCentres (post-map)")
        log_fn("  restoring our own boundary conditions (mapFields also clobbers fixedValue "
               "patches like inlet with interpolated garbage)...")
        restore_boundary_conditions(case_dir, inlet_velocity=inlet_velocity, inlet2_velocity=inlet2_velocity,
                                     has_outlet2=has_outlet2)

    if converge_flow:
        log_fn(f"Converging flow field ({flow_convergence_method}, chunk size="
               f"{simple_foam_iterations} iterations)...")
        converge_flow_field(case_dir, n_iterations=simple_foam_iterations, fan_entry=fan_entry,
                             log_fn=log_fn, should_stop=should_stop, method=flow_convergence_method,
                             rel_tol=flow_rel_tol, max_iterations=flow_max_iterations, solver_log_fn=solver_log_fn)
        if should_stop is not None and should_stop():
            raise StoppedByUser("Stopped after flow convergence.")
        log_fn("  restoring our own boundary conditions again (simpleFoam's mesh-derived "
               "boundary values aren't necessarily our fixedValue settings either)...")
        restore_boundary_conditions(case_dir, inlet_velocity=inlet_velocity, inlet2_velocity=inlet2_velocity,
                                     has_outlet2=has_outlet2)

    log_fn("Computing fluence rate at cell centers...")
    points = read_cell_centers(case_dir, "0")
    values = compute_fluence_at_points(room, points)
    log_fn(f"  {len(points)} cells, fluence rate range [{values.min():.4g}, {values.max():.4g}], "
           f"mean {values.mean():.4g}")
    summary["n_cells"] = len(points)
    summary["fluence_range"] = (float(values.min()), float(values.max()))
    summary["fluence_mean"] = float(values.mean())
    patch_names = read_boundary_patch_names(case_dir)
    write_scalar_field(case_dir, "fluenceRate", values, patch_names)

    log_fn(f"Computing inactivation rate (Z={Z})...")
    k_values = compute_inactivation_rate(values, Z)
    summary["k_range"] = (float(k_values.min()), float(k_values.max()))
    write_scalar_field(case_dir, "kUV", k_values, patch_names)

    eACH_values = compute_well_mixed_eACH(k_values)
    summary["eACH_uv_well_mixed_mean"] = float(eACH_values.mean())
    summary["eACH_uv_well_mixed_range"] = (float(eACH_values.min()), float(eACH_values.max()))
    log_fn(f"  eACH_UV well-mixed (volume-averaged) = {summary['eACH_uv_well_mixed_mean']:.4g} /hr "
           f"(vs. ventilation ach={ach} /hr)")

    log_fn(f"Binning into {nbins} cellZones...")
    bin_idx, bin_repr = bin_decay_rates(k_values, nbins)
    zone_names, _ = write_cellzones(case_dir, bin_idx, nbins)
    write_fvoptions(case_dir, zone_names, bin_repr, field_name=source_field)
    summary["n_zones"] = int(sum(1 for b in range(len(zone_names)) if (bin_idx == b).any() and b > 0))

    if fan_entry is not None:
        log_fn("  Re-carving fan cellZone (write_cellzones() above overwrote constant/polyMesh/cellZones "
               "from scratch, wiping it - topoSet's own merge behavior restores it deterministically, "
               "same cylinder selection since the mesh hasn't changed)...")
        _run_wsl_or_raise("topoSet -dict system/fanTopoSetDict", case_dir_wsl, "topoSet (restore fan zone)")
        log_fn("  Appending fan entry to fvOptions (stays active for the pimpleFoam phase too)...")
        with open(f"{case_dir}/constant/fvOptions", "a") as f:
            f.write(fan_entry)

    log_fn("Splicing fvOptions into controlDict...")
    _, n_open, n_close = splice_fv_options_into_control_dict(case_dir)
    if n_open != n_close:
        raise RuntimeError(f"Brace mismatch after splice: open={n_open} close={n_close}")
    log_fn(f"  Brace check OK ({{={n_open}, }}={n_close})")

    log_fn(f"Setting pimpleFoam transient run parameters: endTime={pimple_end_time}s, "
           f"writeInterval={pimple_write_interval}s, deltaT={pimple_delta_t}s...")
    set_control_dict_time(case_dir, end_time=pimple_end_time,
                           write_interval=pimple_write_interval, delta_t=pimple_delta_t)
    summary["pimple_end_time"] = pimple_end_time
    summary["pimple_write_interval"] = pimple_write_interval

    log_fn("Case setup complete.")
    return summary
