import json
import threading

from guvcfd import project_status as ps


def _room(x=4.0, y=5.0, z=2.7):
    return type("Room", (), {"x": x, "y": y, "z": z})()


def _flow_settings(**overrides):
    settings = {
        "inlet-wall": "xMin", "inlet-y-input": 2.5, "inlet-z-input": 0.4,
        "inlet-size-w": 0.4, "inlet-size-h": 0.4, "inlet-diffuser-type": "ceiling",
        "inlet2-enable": False, "inlet2-wall": "ceiling", "inlet2-y-input": 1.5, "inlet2-z-input": 1.5,
        "inlet2-size-w": 0.3, "inlet2-size-h": 0.3, "inlet2-diffuser-type": "direct",
        "outlet-wall": "xMax", "outlet-y-input": 2.5, "outlet-z-input": 2.7,
        "outlet-size-w": 0.4, "outlet-size-h": 0.4,
        "outlet2-enable": False, "outlet2-wall": "floor", "outlet2-y-input": 1.5, "outlet2-z-input": 1.5,
        "outlet2-size-w": 0.3, "outlet2-size-h": 0.3,
        "ach": 3.0, "mesh-cell-size": 0.1, "momentum-relaxation": 0.7, "scalar-relaxation": 0.7,
        "fan-enable": False, "fan-speed": 0.4, "fan-direction": "down", "fan-radius": 0.45,
        "fan-thickness": 0.2, "fan-x-input": 2.0, "fan-y-input": 1.5, "fan-z-input": 2.2,
    }
    settings.update(overrides)
    return settings


def test_flow_fingerprint_stable_for_identical_inputs():
    a = ps.compute_flow_fingerprint(_flow_settings(), _room())
    b = ps.compute_flow_fingerprint(_flow_settings(), _room())
    assert a == b


def test_flow_fingerprint_changes_with_ach():
    a = ps.compute_flow_fingerprint(_flow_settings(ach=3.0), _room())
    b = ps.compute_flow_fingerprint(_flow_settings(ach=6.0), _room())
    assert a != b


def test_flow_fingerprint_changes_with_room_geometry():
    a = ps.compute_flow_fingerprint(_flow_settings(), _room(x=4.0))
    b = ps.compute_flow_fingerprint(_flow_settings(), _room(x=5.0))
    assert a != b


def test_flow_fingerprint_unaffected_by_lamp_or_uv_settings():
    # z-value/uv-zone-bins/etc. aren't in FLOW_FINGERPRINT_FIELDS at all -
    # changing them must not change the flow fingerprint, since they don't
    # affect the mesh/flow field/ventilation measurement.
    a = ps.compute_flow_fingerprint(_flow_settings(**{"z-value": 2.0, "uv-zone-bins": 25}), _room())
    b = ps.compute_flow_fingerprint(_flow_settings(**{"z-value": 8.0, "uv-zone-bins": 50}), _room())
    assert a == b


def _write_kuv(case_dir, values):
    lines = ["FoamFile", "{", "}", "", "internalField   nonuniform List<scalar>",
             str(len(values)), "("]
    lines += [f"{v}" for v in values]
    lines += [")", ";"]
    (case_dir / "0").mkdir(parents=True, exist_ok=True)
    (case_dir / "0" / "kUV").write_text("\n".join(lines))


def test_uv_fingerprint_stable_and_sensitive_to_field_values(tmp_path):
    case_a = tmp_path / "a"
    case_b = tmp_path / "b"
    _write_kuv(case_a, [0.001, 0.002, 0.003])
    _write_kuv(case_b, [0.001, 0.002, 0.003])
    assert ps.compute_uv_fingerprint(str(case_a)) == ps.compute_uv_fingerprint(str(case_b))

    case_c = tmp_path / "c"
    _write_kuv(case_c, [0.001, 0.002, 0.999])
    assert ps.compute_uv_fingerprint(str(case_a)) != ps.compute_uv_fingerprint(str(case_c))


def test_load_project_status_returns_empty_skeleton_when_missing(tmp_path):
    status = ps.load_project_status(str(tmp_path), "myproj", guv_path="g.guv",
                                     settings_path="g.guvcfd", sim_type="decay")
    assert status["guv_path"] == "g.guv"
    assert status["settings_path"] == "g.guvcfd"
    assert status["case_dir"] == str(tmp_path)
    assert status["sim_type"] == "decay"
    assert status["combos"] == {}


def test_update_combo_status_creates_and_persists(tmp_path):
    ps.update_combo_status(str(tmp_path), "myproj", z=3.0, ach=6.0, guv_path="g.guv",
                            settings_path="g.guvcfd", sim_type="decay",
                            status="running", flow_fingerprint="abc123")

    on_disk = json.loads((tmp_path / "myproj_status.json").read_text())
    combo = on_disk["combos"]["Z3_ACH6"]
    assert combo["z"] == 3.0 and combo["ach"] == 6.0
    assert combo["status"] == "running"
    assert combo["flow_fingerprint"] == "abc123"
    assert "last_updated" in on_disk


def test_update_combo_status_merges_not_overwrites_other_fields(tmp_path):
    ps.update_combo_status(str(tmp_path), "myproj", z=3.0, ach=6.0,
                            status="running", flow_fingerprint="abc123")
    ps.update_combo_status(str(tmp_path), "myproj", z=3.0, ach=6.0,
                            status="done", uv_fingerprint="def456")

    status = ps.load_project_status(str(tmp_path), "myproj")
    combo = status["combos"]["Z3_ACH6"]
    assert combo["status"] == "done"
    assert combo["flow_fingerprint"] == "abc123"  # preserved from the first call
    assert combo["uv_fingerprint"] == "def456"


def test_update_combo_status_top_level_fields_never_overwritten_once_set(tmp_path):
    ps.update_combo_status(str(tmp_path), "myproj", z=3.0, ach=6.0,
                            guv_path="original.guv", status="running")
    # A later call passes a DIFFERENT guv_path - must not clobber the
    # already-recorded one (matches capture_openfoam_settings' own
    # never-overwrite-what's-there contract).
    ps.update_combo_status(str(tmp_path), "myproj", z=5.0, ach=6.0,
                            guv_path="different.guv", status="running")

    status = ps.load_project_status(str(tmp_path), "myproj")
    assert status["guv_path"] == "original.guv"


def test_update_combo_status_is_safe_under_concurrent_writers(tmp_path):
    # Regression guard: several combos updating the SAME project status
    # file concurrently (matches _run_sweep_concurrent's real usage
    # pattern) must not lose updates to a race.
    def worker(i):
        ps.update_combo_status(str(tmp_path), "myproj", z=float(i), ach=6.0, status="done")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    status = ps.load_project_status(str(tmp_path), "myproj")
    assert len(status["combos"]) == 20
    assert all(c["status"] == "done" for c in status["combos"].values())


# --- update_ach_base_status / find_reusable_ach_base (2026-08-10) ---

def _make_base_dirs(tmp_path, ach_label="3"):
    base_dir = tmp_path / f"_base_ACH{ach_label}"
    control_dir = tmp_path / f"_control_ACH{ach_label}"
    base_dir.mkdir()
    control_dir.mkdir()
    return str(base_dir), str(control_dir)


def test_find_reusable_ach_base_none_when_nothing_recorded(tmp_path):
    assert ps.find_reusable_ach_base(str(tmp_path), "myproj", 3.0, "fp1") is None


def test_find_reusable_ach_base_returns_record_on_matching_fingerprint(tmp_path):
    base_dir, control_dir = _make_base_dirs(tmp_path)
    ps.update_ach_base_status(str(tmp_path), "myproj", 3.0, "fp1", base_dir, control_dir,
                               {"total_ach_effective": 3.0})

    record = ps.find_reusable_ach_base(str(tmp_path), "myproj", 3.0, "fp1")
    assert record is not None
    assert record["base_dir"] == base_dir and record["control_dir"] == control_dir
    assert record["control_results"] == {"total_ach_effective": 3.0}


def test_find_reusable_ach_base_none_on_fingerprint_mismatch(tmp_path):
    base_dir, control_dir = _make_base_dirs(tmp_path)
    ps.update_ach_base_status(str(tmp_path), "myproj", 3.0, "fp1", base_dir, control_dir, {})

    assert ps.find_reusable_ach_base(str(tmp_path), "myproj", 3.0, "fp2") is None


def test_find_reusable_ach_base_none_when_directories_no_longer_exist(tmp_path):
    base_dir, control_dir = _make_base_dirs(tmp_path)
    ps.update_ach_base_status(str(tmp_path), "myproj", 3.0, "fp1", base_dir, control_dir, {})

    # Simulate a manual cleanup / "Clean up shared scratch directories"
    # action that removed the directories without touching the status
    # file - the stale JSON record alone must not be trusted.
    import shutil
    shutil.rmtree(base_dir)

    assert ps.find_reusable_ach_base(str(tmp_path), "myproj", 3.0, "fp1") is None


def test_find_reusable_ach_base_keyed_per_ach_not_shared_across_achs(tmp_path):
    base3, control3 = _make_base_dirs(tmp_path, "3")
    base6, control6 = _make_base_dirs(tmp_path, "6")
    ps.update_ach_base_status(str(tmp_path), "myproj", 3.0, "fp1", base3, control3, {})
    ps.update_ach_base_status(str(tmp_path), "myproj", 6.0, "fp1", base6, control6, {})

    assert ps.find_reusable_ach_base(str(tmp_path), "myproj", 3.0, "fp1")["base_dir"] == base3
    assert ps.find_reusable_ach_base(str(tmp_path), "myproj", 6.0, "fp1")["base_dir"] == base6


def test_find_reusable_ach_base_handles_sealed_ach_label(tmp_path):
    base_dir, control_dir = _make_base_dirs(tmp_path, "sealed")
    ps.update_ach_base_status(str(tmp_path), "myproj", 0.0, "fp1", base_dir, control_dir, {})

    assert ps.find_reusable_ach_base(str(tmp_path), "myproj", 0.0, "fp1") is not None


def test_update_ach_base_status_persists_to_disk(tmp_path):
    base_dir, control_dir = _make_base_dirs(tmp_path)
    ps.update_ach_base_status(str(tmp_path), "myproj", 3.0, "fp1", base_dir, control_dir, {"x": 1})

    on_disk = json.loads((tmp_path / "myproj_status.json").read_text())
    record = on_disk["ach_bases"]["3"]
    assert record["flow_fingerprint"] == "fp1"
    assert record["control_results"] == {"x": 1}
