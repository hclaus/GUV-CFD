

def test_staging_dir_is_unique_per_control_so_concurrent_clones_dont_race(monkeypatch):
    """Regression: the staging path used to be derived from the SOURCE case
    only (f"{case_dir}-no-uv-staging"), so two controls cloned from the same
    base concurrently computed the identical path and raced - one mv consumed
    the directory and the other died with "cannot stat ...-no-uv-staging".

    Harmless while the control was source-independent (one per ACH group), but
    the control now carries the breathing inlet and so depends on the source
    position, making two controls off one base a real case.
    """
    import guvcfd.ventilation_control as vc

    cmds = []
    monkeypatch.setattr(vc, "run_wsl_or_raise", lambda cmd, cwd, step: cmds.append(cmd))
    for name in ("restore_boundary_conditions", "write_fvoptions_file",
                 # set_relaxation_factors edits system/fvSolution, which this
                 # test's fake case dir does not have - it is exercising the
                 # staging path only. That the decay relaxation IS applied is
                 # covered by test_decay_scalar_relaxation.
                 "set_relaxation_factors",
                 "set_function_object_enabled", "set_control_dict_start_from",
                 "set_control_dict_time", "splice_live_vol_average_if_needed"):
        monkeypatch.setattr(vc, name, lambda *a, **k: None)
    monkeypatch.setattr(vc, "splice_fv_options_into_control_dict", lambda d: (None, 1, 1))

    def staging_paths_for(control_dir):
        cmds.clear()
        vc.prepare_ventilation_only_control(
            "/run/_base_ACH6", control_dir, (0.1, 0, 0), 100, 10, log_fn=lambda m: None)
        return [c for c in cmds if "no-uv-staging" in c]

    a = staging_paths_for("/run/_control_A")
    b = staging_paths_for("/run/_control_B")
    assert a and b
    # same source base, different controls -> the staging paths must differ
    assert not (set(a) & set(b)), f"staging paths collide:\n  A={a}\n  B={b}"
