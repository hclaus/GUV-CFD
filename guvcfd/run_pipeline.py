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
from .fan import write_fan_topo_set_dict, fan_fvoptions_entry
from .fluence import compute_fluence_at_points, compute_inactivation_rate, compute_well_mixed_eACH
from .initial_fields import write_initial_fields, compute_inlet_velocity, restore_boundary_conditions
from .mesh_gen import write_mesh_dicts, write_map_fields_dict
from .splice import (
    splice_fv_options_into_control_dict,
    set_function_object_enabled,
    set_control_dict_time,
    ensure_simple_fvsolution,
)
from .wsl_utils import wsl_path as _wsl_path, run_wsl as _run_wsl, run_wsl_or_raise as _run_wsl_or_raise


def converge_flow_field(case_dir, n_iterations=500, fan_entry=None, log_fn=print):
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
    """
    case_dir_wsl = _wsl_path(case_dir)

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

    log_fn(f"Setting simpleFoam iteration budget: {n_iterations} iterations "
           f"(writing only the final state)...")
    set_control_dict_time(case_dir, end_time=n_iterations, write_interval=n_iterations, delta_t=1)

    log_fn("Running simpleFoam (this can take a while)...")
    r = _run_wsl("rm -f log.simpleFoam; simpleFoam > log.simpleFoam 2>&1", case_dir_wsl)
    tail = _run_wsl("tail -20 log.simpleFoam", case_dir_wsl).stdout
    log_fn(tail)
    if r.returncode != 0 or "FOAM FATAL" in tail or "Floating Point Exception" in tail:
        raise RuntimeError(f"simpleFoam failed (exit {r.returncode}):\n{tail}")

    r = _run_wsl_or_raise(
        "ls -d [0-9]*/ 2>/dev/null | sed 's#/##' | sort -n | tail -1",
        case_dir_wsl, "listing time directories",
    )
    latest = r.stdout.strip()
    if not latest or latest == "0":
        raise RuntimeError(f"simpleFoam did not write any new time directory (found: {latest!r})")
    log_fn(f"  simpleFoam stopped at time {latest}. Copying converged fields back into 0/ "
           f"(excluding T - that's our fresh UV-decay starting condition, not a flow quantity)...")

    r = _run_wsl_or_raise(f"ls {latest}/ | grep -v '^uniform$' | grep -v '^T$'",
                           case_dir_wsl, "listing converged field files")
    field_files = r.stdout.split()
    log_fn(f"  Fields: {field_files}")
    cp_targets = " ".join(f"{latest}/{f}" for f in field_files)
    _run_wsl_or_raise(f"cp -f {cp_targets} 0/", case_dir_wsl, "copying converged fields")
    _run_wsl_or_raise(f"rm -rf {latest}", case_dir_wsl, "cleaning up iteration directory")

    log_fn("Re-enabling scalarTransport1 for the transient UV-decay run...")
    set_function_object_enabled(case_dir, "scalarTransport1", True)

    return latest


_WALL_INFLOW_DIRECTION = {"xMin": (1, 0, 0), "xMax": (-1, 0, 0)}


def setup_case(guv_path, case_dir, template_case_dir=None, cell_size=0.1, Z=2.0, nbins=25,
               source_field="T", map_from_case=None, map_from_time=0, ach=3.0,
               inlet_wall="xMin", inlet_center=(0.5, 0.85), inlet_size=(0.3, 0.3),
               outlet_wall="xMax", outlet_center=(0.5, 0.15), outlet_size=(0.3, 0.3),
               converge_flow=True, simple_foam_iterations=500,
               pimple_end_time=120, pimple_write_interval=10, pimple_delta_t=0.5,
               fan_speed=None, fan_center=None, fan_direction=(0, 0, -1),
               fan_disk_radius=0.4, fan_disk_thickness=0.2, fan_height=None,
               log_fn=print):
    """Set up an OpenFOAM case end-to-end from a .guv project. Returns a dict
    summarizing the run (room dims, lamp count, fluence/k ranges, zone count).

    ach: target air changes per hour [1/hr] - inlet velocity is derived from
    this and the room volume/inlet area (see initial_fields.compute_inlet_velocity),
    rather than a fixed velocity that would silently drift ACH as room size changes.

    converge_flow: if True, run simpleFoam (see converge_flow_field()) to get
    a genuinely converged flow field for this mesh, rather than trusting
    mapFields' interpolated guess as-is.

    pimple_end_time/pimple_write_interval/pimple_delta_t: the transient
    UV-decay run's simulated duration [s], write cadence [s], and time step
    [s] - these (along with Z and ach above) are all destined to become GUI
    input fields; keeping them as plain function arguments here rather than
    hardcoded so that wiring is a small change, not a rework.

    fan_speed: if given (m/s, see fan.SPEED_RANGE), adds an optional mixing
    fan (see fan.py) - a small cylindrical cellZone near the ceiling with a
    meanVelocityForce driving that zone's mean velocity to fan_speed in
    fan_direction. Stays active through flow convergence *and* the
    pimpleFoam phase (a real fan affects the whole scenario, not just part
    of it) - unlike the UV/source entries, which only apply once scalar
    transport starts. fan_center defaults to room center at 85% of room
    height if not given.
    """
    case_dir_wsl = _wsl_path(case_dir)
    summary = {}

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

    log_fn(f"Loading project {guv_path} ...")
    project = Project.load(guv_path)
    room = next(iter(project.rooms.values()))
    log_fn(f"  Room {room.x}x{room.y}x{room.z} {room.units}, {len(room.lamps)} lamp(s)")
    summary["room"] = (room.x, room.y, room.z, str(room.units))
    summary["n_lamps"] = len(room.lamps)

    log_fn("Writing mesh dicts (blockMeshDict/topoSetDict/createPatchDict)...")
    write_mesh_dicts(case_dir, room.x, room.y, room.z, cell_size=cell_size,
                      inlet_wall=inlet_wall, inlet_center=inlet_center, inlet_size=inlet_size,
                      outlet_wall=outlet_wall, outlet_center=outlet_center, outlet_size=outlet_size)

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
        center = fan_center or (room.x / 2, room.y / 2, (fan_height if fan_height is not None else 0.85 * room.z))
        p1 = (center[0], center[1], center[2] - fan_disk_thickness / 2)
        p2 = (center[0], center[1], center[2] + fan_disk_thickness / 2)
        log_fn(f"Carving fan cellZone at {center}, radius={fan_disk_radius}, speed={fan_speed} m/s...")
        write_fan_topo_set_dict(case_dir, p1, p2, fan_disk_radius)
        _run_wsl_or_raise("topoSet -dict system/fanTopoSetDict", case_dir_wsl, "topoSet (fan zone)")
        fan_entry = fan_fvoptions_entry(fan_speed, direction=fan_direction)
        summary["fan"] = {"center": center, "speed": fan_speed, "direction": fan_direction}

    room_volume = room.x * room.y * room.z
    inlet_area = inlet_size[0] * inlet_size[1]
    inflow_dir = _WALL_INFLOW_DIRECTION[inlet_wall]
    v_mag = compute_inlet_velocity(ach, room_volume, inlet_area)
    inlet_velocity = tuple(v_mag * d for d in inflow_dir)
    log_fn(f"Writing initial fields (0/{{U,p,k,omega,nut,T}}), ACH={ach} -> "
           f"inlet velocity {inlet_velocity} m/s (room volume={room_volume:.3g} m^3, "
           f"inlet area={inlet_area:.3g} m^2)...")
    Path(f"{case_dir}/0").mkdir(parents=True, exist_ok=True)
    write_initial_fields(case_dir, inlet_velocity=inlet_velocity)
    summary["ach"] = ach
    summary["inlet_velocity"] = inlet_velocity

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
        restore_boundary_conditions(case_dir, inlet_velocity=inlet_velocity)

    if converge_flow:
        log_fn(f"Converging flow field (simpleFoam, budget={simple_foam_iterations} iterations)...")
        converge_flow_field(case_dir, n_iterations=simple_foam_iterations, fan_entry=fan_entry, log_fn=log_fn)
        log_fn("  restoring our own boundary conditions again (simpleFoam's mesh-derived "
               "boundary values aren't necessarily our fixedValue settings either)...")
        restore_boundary_conditions(case_dir, inlet_velocity=inlet_velocity)

    log_fn("Computing fluence rate at cell centers...")
    points = read_cell_centers(case_dir, "0")
    values = compute_fluence_at_points(room, points)
    log_fn(f"  {len(points)} cells, fluence rate range [{values.min():.4g}, {values.max():.4g}]")
    summary["n_cells"] = len(points)
    summary["fluence_range"] = (float(values.min()), float(values.max()))
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
