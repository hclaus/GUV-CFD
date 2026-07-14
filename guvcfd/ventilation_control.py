"""Run a ventilation-only control: clone an already-set-up case's mesh and
converged flow field into a subfolder, strip out the UV source entirely, and
rerun just the transient decay - to measure the *actual* CFD air-change
efficiency achieved by ventilation alone (as opposed to the nominal ACH used
to set the inlet boundary condition, which imperfect real-world mixing
doesn't fully deliver on - see decay_analysis.compute_effective_eACH's
measured_ventilation_lambda_per_s parameter, which this feeds).
"""
from .contaminant_source import write_fvoptions_file
from .decay_analysis import write_results_summary
from .initial_fields import compute_inlet_velocities, restore_boundary_conditions
from .monitoring import write_vol_average_dict
from .splice import (
    splice_fv_options_into_control_dict,
    set_function_object_enabled,
    set_control_dict_start_from,
    set_control_dict_time,
)
from .wsl_utils import wsl_path, run_wsl_or_raise, run_wsl_streaming, StoppedByUser


def run_ventilation_only_control(case_dir, control_dir, ach, room_x, room_y, room_z,
                                  inlet_wall, inlet_size, pimple_end_time,
                                  pimple_write_interval, pimple_delta_t=0.5,
                                  inlet2_wall=None, inlet2_size=None, has_outlet2=False,
                                  log_fn=print, should_stop=None, solver_log_fn=None):
    """Clone case_dir's mesh/converged flow field into control_dir, remove
    every UV source, reset T fresh, and run the transient decay driven by
    ventilation alone. Returns the control run's results dict (ventilation_ach
    set, eACH_uv_well_mixed=0.0) - its total_ach_effective is the actual
    measured ventilation air-change rate.

    inlet2_wall/inlet2_size/has_outlet2: mirror whatever 2nd inlet/outlet
    the original case_dir was actually built with (see setup_case) - the
    mesh is cloned as-is, so these only need to match for the boundary
    condition *values* to come out right, not to change the mesh itself.
    """
    control_dir_wsl = wsl_path(control_dir)
    case_dir_wsl_src = wsl_path(case_dir)

    log_fn(f"Cloning {case_dir} mesh/flow field into {control_dir} (UV-off control)...")
    # control_dir is nested *inside* case_dir (a "no UV" subfolder) - cp -r
    # into a destination that's already inside the source tree fails ("cannot
    # copy a directory into itself"). Copy to a sibling staging dir first
    # (definitely outside the source tree), then mv it into its final nested
    # location - mv on the same filesystem is a metadata rename, not a
    # second full copy.
    staging_wsl = f"{case_dir_wsl_src}-no-uv-staging"
    run_wsl_or_raise(f'rm -rf "{staging_wsl}"', "$HOME", "clearing any stale staging dir")
    run_wsl_or_raise(f'cp -r "{case_dir_wsl_src}" "{staging_wsl}"', "$HOME",
                      "copying case into staging dir")
    run_wsl_or_raise(f'rm -rf "{control_dir_wsl}"', "$HOME", "clearing any stale control dir")
    run_wsl_or_raise(f'mv "{staging_wsl}" "{control_dir_wsl}"', "$HOME",
                      "moving staged clone into place")
    run_wsl_or_raise(
        'for d in [0-9]*/; do [ "$d" = "0/" ] || rm -rf "$d"; done '
        '&& rm -rf postProcessing results.json log.pimpleFoam log.simpleFoam run_settings.json',
        control_dir_wsl, "stripping non-mesh/flow-field state from the clone",
    )
    if should_stop is not None and should_stop():
        raise StoppedByUser("Stopped before UV-off control run.")

    room_volume = room_x * room_y * room_z
    openings = [(inlet_wall, inlet_size[0] * inlet_size[1])]
    if inlet2_wall is not None:
        openings.append((inlet2_wall, inlet2_size[0] * inlet2_size[1]))
    velocities = compute_inlet_velocities(ach, room_volume, openings)
    inlet_velocity = velocities[0]
    inlet2_velocity = velocities[1] if inlet2_wall is not None else None

    log_fn("Resetting T to a fresh initial condition (U/p/k/omega/nut untouched)...")
    restore_boundary_conditions(control_dir, inlet_velocity=inlet_velocity,
                                 inlet2_velocity=inlet2_velocity, has_outlet2=has_outlet2)

    log_fn("Writing an empty constant/fvOptions (no UV source - ventilation only)...")
    write_fvoptions_file(control_dir, [])

    log_fn("Ensuring scalarTransport1 is enabled...")
    set_function_object_enabled(control_dir, "scalarTransport1", True)

    log_fn("Splicing the (now empty) fvOptions into controlDict...")
    _, n_open, n_close = splice_fv_options_into_control_dict(control_dir)
    if n_open != n_close:
        raise RuntimeError(f"Brace mismatch after splice: open={n_open} close={n_close}")

    set_control_dict_start_from(control_dir, "startTime")
    set_control_dict_time(control_dir, end_time=pimple_end_time,
                           write_interval=pimple_write_interval, delta_t=pimple_delta_t)

    log_fn(f"Running pimpleFoam (UV-off control) to {pimple_end_time}s...")
    r = run_wsl_streaming(
        "pimpleFoam 2>&1 | tee log.pimpleFoam", control_dir_wsl,
        on_line=solver_log_fn or log_fn, should_stop=should_stop, kill_pattern="pimpleFoam",
    )
    if should_stop is not None and should_stop():
        raise StoppedByUser("Stopped during UV-off control pimpleFoam.")
    if r.returncode != 0 or "FOAM FATAL" in r.stdout or "Floating Point Exception" in r.stdout:
        tail = "\n".join(r.stdout.splitlines()[-25:]) or "(no output captured)"
        raise RuntimeError(f"UV-off control pimpleFoam failed (exit {r.returncode}):\n{tail}")

    log_fn("Post-processing the control run (volAverage T)...")
    write_vol_average_dict(control_dir)
    run_wsl_or_raise("rm -rf postProcessing", control_dir_wsl, "clearing postProcessing")
    run_wsl_or_raise("postProcess -dict system/volAverageDict", control_dir_wsl, "postProcess volAverage")

    log_fn("Writing the control run's results.json...")
    results = write_results_summary(control_dir, f"{control_dir}/results.json", ach, 0.0)
    log_fn(f"UV-off control done: measured ventilation ACH = "
           f"{results['total_ach_effective']:.4g} /hr (nominal was {ach:.4g} /hr).")
    return results
