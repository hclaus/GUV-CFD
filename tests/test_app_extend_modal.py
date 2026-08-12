from types import SimpleNamespace

from guvcfd import app as guvcfd_app
from guvcfd.project_status import update_ach_base_status, update_combo_status


def _reset_run_states():
    guvcfd_app._run_state["status"] = "idle"
    guvcfd_app._scenario_state["status"] = "idle"
    guvcfd_app._pending_extend_modal.update(
        project_dir=None, project_name=None, guv_path=None, settings_path=None,
        settings=None, room=None, status=None, action=None,
    )
    guvcfd_app._pending_extend_cleanup["dirs"] = None


# --- _load_extend_status / _refresh_extend_view ---

def test_load_extend_status_reports_missing_status_file(tmp_path):
    status, settings, room, guv_path, settings_path, error = guvcfd_app._load_extend_status(str(tmp_path))
    assert error is not None and "No recorded status" in error
    assert settings is None and room is None


def test_load_extend_status_loads_settings_and_room(tmp_path, monkeypatch):
    settings_path = tmp_path / "proj.guvcfd"
    settings_path.write_text('{"sim-type": "decay", "ach": 3}')
    update_combo_status(str(tmp_path), tmp_path.name, z=6.0, ach=3.0, guv_path="proj.guv",
                         settings_path=str(settings_path), sim_type="decay", status="done")

    fake_room = SimpleNamespace(x=4.0, y=5.0, z=2.7)
    fake_project = SimpleNamespace(rooms={"r": fake_room})
    monkeypatch.setattr(guvcfd_app.Project, "load", staticmethod(lambda path: fake_project))

    status, settings, room, guv_path, sp, error = guvcfd_app._load_extend_status(str(tmp_path))
    assert error is None
    assert settings == {"sim-type": "decay", "ach": 3}
    assert room is fake_room
    assert guv_path == "proj.guv"


def test_refresh_extend_view_prefills_sweep_fields_and_dropdown(tmp_path, monkeypatch):
    settings_path = tmp_path / "proj.guvcfd"
    settings_path.write_text('{"sim-type": "decay"}')
    for z, ach in [(2.0, 3.0), (6.0, 3.0), (6.0, 6.0)]:
        update_combo_status(str(tmp_path), tmp_path.name, z=z, ach=ach, guv_path="proj.guv",
                             settings_path=str(settings_path), sim_type="decay", status="done")

    fake_room = SimpleNamespace(x=4.0, y=5.0, z=2.7)
    monkeypatch.setattr(guvcfd_app.Project, "load",
                         staticmethod(lambda path: SimpleNamespace(rooms={"r": fake_room})))

    body, msg, sweep_z, sweep_ach, uv_z, uv_guv, options, rebuild_style, mode_style = (
        guvcfd_app._refresh_extend_view(str(tmp_path)))
    assert msg == ""
    assert sweep_z == "2, 6"
    assert sweep_ach == "3, 6"
    # Prefilled with the same Z values as the sweep field (not left blank)
    # so the user sees exactly which ones are about to be re-evaluated
    # under a new .guv design.
    assert uv_z == "2, 6"
    assert uv_guv == "proj.guv"
    assert len(options) == 3
    assert rebuild_style == {"display": "none"}  # combos exist - nothing to rebuild
    # This project's sim-type is "decay" - the mode-switch dropdown is
    # only offered for a steady-state project (see its own layout note).
    assert mode_style == {"display": "none"}
    assert guvcfd_app._pending_extend_modal["project_dir"] == str(tmp_path)
    assert guvcfd_app._pending_extend_modal["room"] is fake_room


def test_refresh_extend_view_shows_mode_dropdown_for_a_steady_state_project(tmp_path, monkeypatch):
    settings_path = tmp_path / "proj.guvcfd"
    settings_path.write_text('{"sim-type": "steady_state"}')
    update_combo_status(str(tmp_path), tmp_path.name, z=6.0, ach=3.0, guv_path="proj.guv",
                         settings_path=str(settings_path), sim_type="steady_state", status="done")
    fake_room = SimpleNamespace(x=4.0, y=5.0, z=2.7)
    monkeypatch.setattr(guvcfd_app.Project, "load",
                         staticmethod(lambda path: SimpleNamespace(rooms={"r": fake_room})))

    *_, mode_style = guvcfd_app._refresh_extend_view(str(tmp_path))
    assert mode_style == {"display": "block"}


def test_refresh_extend_view_offers_rebuild_when_no_status_file_exists(tmp_path):
    body, msg, sweep_z, sweep_ach, uv_z, uv_guv, options, rebuild_style, mode_style = (
        guvcfd_app._refresh_extend_view(str(tmp_path)))
    assert rebuild_style == {"display": "block"}


# --- browse buttons pass the current field value as initialdir ---

def test_browse_extend_project_dir_uses_current_field_as_initialdir(monkeypatch):
    captured = {}
    monkeypatch.setattr(guvcfd_app, "_native_choose_dir",
                         lambda title, initialdir=None: captured.update(initialdir=initialdir) or None)
    guvcfd_app._browse_extend_project_dir(1, r"C:\some\existing\folder")
    assert captured["initialdir"] == r"C:\some\existing\folder"


def test_browse_extend_uv_guv_uses_current_field_parent_as_initialdir(monkeypatch):
    captured = {}
    monkeypatch.setattr(guvcfd_app, "_native_open_file",
                         lambda filetypes, title, initialdir=None: captured.update(initialdir=initialdir) or None)
    guvcfd_app._browse_extend_uv_guv(1, r"C:\projects\room1\room1.guv")
    assert captured["initialdir"] == r"C:\projects\room1"


def test_browse_extend_uv_guv_none_initialdir_when_field_empty(monkeypatch):
    captured = {}
    monkeypatch.setattr(guvcfd_app, "_native_open_file",
                         lambda filetypes, title, initialdir=None: captured.update(initialdir=initialdir) or None)
    guvcfd_app._browse_extend_uv_guv(1, None)
    assert captured["initialdir"] is None


# --- _create_extend_status_from_disk ---

def test_create_extend_status_from_disk_reports_nothing_found(tmp_path):
    result = guvcfd_app._create_extend_status_from_disk(1, str(tmp_path))
    assert "No existing run folders" in result[1]


def test_create_extend_status_from_disk_requires_a_folder():
    result = guvcfd_app._create_extend_status_from_disk(1, None)
    assert result[1] == "Enter a project folder first."


def test_create_extend_status_from_disk_rebuilds_and_reloads(tmp_path, monkeypatch):
    import json as _json
    case_dir = tmp_path / "Z6_ACH3"
    case_dir.mkdir()
    settings_path = tmp_path / "old.guvcfd"
    settings_path.write_text('{"sim-type": "decay"}')
    (case_dir / "run_settings.json").write_text(_json.dumps({
        "sim-type": "decay", "z-value": 6.0, "ach": 3.0, "guv_path": "old.guv",
        "settings_path": str(settings_path), "inlet-wall": "xMin",
        "inlet-size-w": 0.3, "inlet-size-h": 0.3,
    }))
    (case_dir / "results.json").write_text("{}")

    fake_room = SimpleNamespace(x=4.0, y=5.0, z=2.7)
    monkeypatch.setattr(guvcfd_app.Project, "load",
                         staticmethod(lambda path: SimpleNamespace(rooms={"r": fake_room})))

    body, msg, sweep_z, sweep_ach, uv_z, uv_guv, options, rebuild_style, mode_style = (
        guvcfd_app._create_extend_status_from_disk(1, str(tmp_path)))

    assert "Rebuilt a status file from 1 existing run folder(s)." in msg
    assert sweep_z == "6" and sweep_ach == "3"
    assert uv_z == "6"
    assert rebuild_style == {"display": "none"}  # now has combos - nothing left to rebuild
    assert mode_style == {"display": "none"}  # rebuilt project's own sim-type is "decay"


# --- _extend_status_table ---

def test_extend_status_table_shows_message_when_no_combos():
    body = guvcfd_app._extend_status_table({"combos": {}})
    assert "No recorded runs" in body.children


def test_extend_status_table_renders_a_row_per_combo():
    status = {"combos": {"Z6_ACH3": {"z": 6.0, "ach": 3.0, "status": "done",
                                       "flow_fingerprint": "abc", "uv_fingerprint": "def"}}}
    table = guvcfd_app._extend_status_table(status)
    # dbc.Table -> [Thead, Tbody]; Tbody -> [Tr] -> [Td...]
    tbody = table.children[1]
    row = tbody.children[0]
    assert row.children[0].children == "6.0"
    assert row.children[2].children == "done"


def test_extend_status_table_omits_started_finished_columns():
    status = {"combos": {"Z6_ACH3": {"z": 6.0, "ach": 3.0, "status": "done"}}}
    table = guvcfd_app._extend_status_table(status)
    headers = [th.children for th in table.children[0].children.children]
    assert headers == ["Z", "ACH", "Status", "Design", "Mode", "Flow", "UV", "Control (ACH)", "Phase 1"]


def test_extend_status_table_shows_which_guv_design_produced_each_combo():
    # Two designs at the SAME Z/ACH would look identical without this
    # column - see compute_guv_design_suffix's own docstring.
    status = {"combos": {
        "Z6_ACH3": {"z": 6.0, "ach": 3.0, "status": "done", "guv_path": "C:/rooms/lampA.guv"},
        "Z6_ACH3_lampB": {"z": 6.0, "ach": 3.0, "status": "done", "guv_path": "C:/rooms/lampB.guv"},
    }}
    table = guvcfd_app._extend_status_table(status)
    rows = table.children[1].children
    designs = {row.children[3].children for row in rows}
    assert designs == {"lampA", "lampB"}


def test_extend_status_table_shows_which_sim_mode_each_combo_ran_under():
    # A steady-state project's combo re-evaluated in decay mode would
    # look identical to the original without this column - see
    # compute_sim_type_suffix's own docstring.
    status = {"combos": {
        "Z6_ACH3": {"z": 6.0, "ach": 3.0, "status": "done", "sim_type": "steady_state"},
        "Z6_ACH3_decay": {"z": 6.0, "ach": 3.0, "status": "done", "sim_type": "decay"},
    }}
    table = guvcfd_app._extend_status_table(status)
    rows = table.children[1].children
    modes = {row.children[4].children for row in rows}
    assert modes == {"steady_state", "decay"}


def test_extend_status_table_shows_control_and_phase1_checkmarks_when_dirs_exist(tmp_path):
    (tmp_path / "_control_ACH3").mkdir()
    (tmp_path / "_phase1_ACH3").mkdir()
    status = {"combos": {"Z6_ACH3": {"z": 6.0, "ach": 3.0, "status": "done"}}}

    table = guvcfd_app._extend_status_table(status, str(tmp_path))

    row = table.children[1].children[0]
    assert row.children[7].children == "✓"  # Control (ACH)
    assert row.children[8].children == "✓"  # Phase 1


def test_extend_status_table_blank_control_and_phase1_when_dirs_missing(tmp_path):
    status = {"combos": {"Z6_ACH3": {"z": 6.0, "ach": 3.0, "status": "done"}}}
    table = guvcfd_app._extend_status_table(status, str(tmp_path))
    row = table.children[1].children[0]
    assert row.children[7].children == ""
    assert row.children[8].children == ""


def test_extend_status_table_blank_control_and_phase1_when_no_project_dir():
    status = {"combos": {"Z6_ACH3": {"z": 6.0, "ach": 3.0, "status": "done"}}}
    table = guvcfd_app._extend_status_table(status)  # project_dir=None
    row = table.children[1].children[0]
    assert row.children[7].children == ""
    assert row.children[8].children == ""


# --- action selection ---

def test_select_extend_action_uv_shows_only_uv_section():
    _reset_run_states()
    styles = guvcfd_app._select_extend_action_uv(1)
    assert guvcfd_app._pending_extend_modal["action"] == "uv"
    assert styles[0] == {"display": "block"}
    assert styles[1] == {"display": "none"}
    assert styles[2] == {"display": "none"}


def test_select_extend_action_extend_shows_only_extend_section():
    _reset_run_states()
    styles = guvcfd_app._select_extend_action_extend(1)
    assert guvcfd_app._pending_extend_modal["action"] == "extend"
    assert styles == ({"display": "none"}, {"display": "block"}, {"display": "none"})


def test_select_extend_action_sweep_shows_only_sweep_section():
    _reset_run_states()
    styles = guvcfd_app._select_extend_action_sweep(1)
    assert guvcfd_app._pending_extend_modal["action"] == "sweep"
    assert styles == ({"display": "none"}, {"display": "none"}, {"display": "block"})


# --- cancel ---

def test_cancel_extend_modal_clears_pending_state():
    guvcfd_app._pending_extend_modal.update(project_dir="x", action="uv")
    is_open = guvcfd_app._cancel_extend_modal(1)
    assert is_open is False
    assert guvcfd_app._pending_extend_modal["project_dir"] is None
    assert guvcfd_app._pending_extend_modal["action"] is None


# --- _run_extend_action dispatch ---

def test_run_extend_action_requires_a_project_loaded():
    _reset_run_states()
    is_open, msg, tab, poll_disabled, run_disabled, stop_disabled = guvcfd_app._run_extend_action(
        1, None, None, None, None, None, None, None)
    assert msg == "Load a valid project folder first."


def test_run_extend_action_requires_an_action_chosen(tmp_path):
    _reset_run_states()
    guvcfd_app._pending_extend_modal.update(
        project_dir=str(tmp_path), settings={"sim-type": "decay"}, room=SimpleNamespace(x=1, y=1, z=1),
        status={"combos": {}}, action=None,
    )
    is_open, msg, tab, poll_disabled, run_disabled, stop_disabled = guvcfd_app._run_extend_action(
        1, None, None, None, None, None, None, None)
    assert "Choose one of the 3 actions" in msg


def test_run_extend_action_uv_launches_sweep_with_new_guv_and_z(tmp_path, monkeypatch):
    _reset_run_states()
    combos = {"Z2_ACH3": {"z": 2.0, "ach": 3.0}, "Z2_ACH6": {"z": 2.0, "ach": 6.0}}
    guvcfd_app._pending_extend_modal.update(
        project_dir=str(tmp_path), settings_path="proj.guvcfd",
        settings={"sim-type": "decay", "ach": 3}, room=SimpleNamespace(x=1, y=1, z=1),
        status={"combos": combos}, action="uv",
    )
    monkeypatch.setattr(guvcfd_app, "load_advanced_settings", lambda: {})
    monkeypatch.setattr(guvcfd_app, "merge_project_openfoam_settings", lambda settings, adv: {"mesh-cell-size": 0.1})

    captured = {}

    def fake_launch(guv_path, settings_path, project_dir, room, settings, adv, z_values, ach_values):
        captured.update(guv_path=guv_path, z_values=z_values, ach_values=ach_values)

    monkeypatch.setattr(guvcfd_app, "_launch_scenario_sweep", fake_launch)

    is_open, msg, tab, poll_disabled, run_disabled, stop_disabled = guvcfd_app._run_extend_action(
        1, "new.guv", "8", None, None, None, None, None)

    assert is_open is False and tab == "scenario-runs"
    assert poll_disabled is False and run_disabled is True and stop_disabled is False
    assert captured["guv_path"] == "new.guv"
    assert captured["z_values"] == [8.0]
    assert captured["ach_values"] == [3.0, 6.0]
    assert guvcfd_app._pending_extend_modal["project_dir"] is None  # cleared after dispatch


def test_run_extend_action_uv_launches_sweep_with_multiple_z_values(tmp_path, monkeypatch):
    _reset_run_states()
    combos = {"Z2_ACH3": {"z": 2.0, "ach": 3.0}}
    guvcfd_app._pending_extend_modal.update(
        project_dir=str(tmp_path), settings_path="proj.guvcfd",
        settings={"sim-type": "decay", "ach": 3}, room=SimpleNamespace(x=1, y=1, z=1),
        status={"combos": combos}, action="uv",
    )
    monkeypatch.setattr(guvcfd_app, "load_advanced_settings", lambda: {})
    monkeypatch.setattr(guvcfd_app, "merge_project_openfoam_settings", lambda settings, adv: {"mesh-cell-size": 0.1})

    captured = {}
    monkeypatch.setattr(guvcfd_app, "_launch_scenario_sweep",
                         lambda guv_path, settings_path, project_dir, room, settings, adv, z_values, ach_values:
                         captured.update(z_values=z_values))

    guvcfd_app._run_extend_action(1, "new.guv", "2, 6, 6.5", None, None, None, None, None)
    assert captured["z_values"] == [2.0, 6.0, 6.5]


def test_run_extend_action_uv_blank_z_defaults_to_projects_original_z_values(tmp_path, monkeypatch):
    # The user-facing rule (per the modal's own note): leaving Z blank
    # means "re-evaluate every Z already swept in this project", not
    # "Z=None" or an error - a prior version of this field was a single
    # required number, forcing the user to retype a list they'd already
    # swept once just to apply a different lamp design to it.
    _reset_run_states()
    combos = {"Z2_ACH3": {"z": 2.0, "ach": 3.0}, "Z6_ACH3": {"z": 6.0, "ach": 3.0}}
    guvcfd_app._pending_extend_modal.update(
        project_dir=str(tmp_path), settings_path="proj.guvcfd",
        settings={"sim-type": "decay", "ach": 3}, room=SimpleNamespace(x=1, y=1, z=1),
        status={"combos": combos}, action="uv",
    )
    monkeypatch.setattr(guvcfd_app, "load_advanced_settings", lambda: {})
    monkeypatch.setattr(guvcfd_app, "merge_project_openfoam_settings", lambda settings, adv: {"mesh-cell-size": 0.1})

    captured = {}
    monkeypatch.setattr(guvcfd_app, "_launch_scenario_sweep",
                         lambda guv_path, settings_path, project_dir, room, settings, adv, z_values, ach_values:
                         captured.update(z_values=z_values))

    guvcfd_app._run_extend_action(1, "new.guv", "", None, None, None, None, None)
    assert captured["z_values"] == [2.0, 6.0]


def test_run_extend_action_uv_requires_guv_path(tmp_path):
    _reset_run_states()
    guvcfd_app._pending_extend_modal.update(
        project_dir=str(tmp_path), settings={"sim-type": "decay"}, room=SimpleNamespace(x=1, y=1, z=1),
        status={"combos": {}}, action="uv",
    )
    is_open, msg, tab, poll_disabled, run_disabled, stop_disabled = guvcfd_app._run_extend_action(
        1, None, None, None, None, None, None, None)
    assert "Pick a .guv file first" in msg


def test_run_extend_action_uv_rejects_unparseable_z_list(tmp_path):
    _reset_run_states()
    guvcfd_app._pending_extend_modal.update(
        project_dir=str(tmp_path), settings={"sim-type": "decay"}, room=SimpleNamespace(x=1, y=1, z=1),
        status={"combos": {}}, action="uv",
    )
    is_open, msg, tab, poll_disabled, run_disabled, stop_disabled = guvcfd_app._run_extend_action(
        1, "new.guv", "not-a-number", None, None, None, None, None)
    assert "Can't parse Z value list" in msg


def test_run_extend_action_sweep_launches_with_parsed_z_ach(tmp_path, monkeypatch):
    _reset_run_states()
    guvcfd_app._pending_extend_modal.update(
        project_dir=str(tmp_path), guv_path="proj.guv", settings_path="proj.guvcfd",
        settings={"sim-type": "decay"}, room=SimpleNamespace(x=1, y=1, z=1),
        status={"combos": {}}, action="sweep",
    )
    monkeypatch.setattr(guvcfd_app, "load_advanced_settings", lambda: {})
    monkeypatch.setattr(guvcfd_app, "merge_project_openfoam_settings", lambda settings, adv: {})

    captured = {}

    def fake_launch(guv_path, settings_path, project_dir, room, settings, adv, z_values, ach_values):
        captured.update(z_values=z_values, ach_values=ach_values)

    monkeypatch.setattr(guvcfd_app, "_launch_scenario_sweep", fake_launch)

    is_open, msg, tab, poll_disabled, run_disabled, stop_disabled = guvcfd_app._run_extend_action(
        1, None, None, None, None, None, "2, 6", "3, 6")

    assert poll_disabled is False and run_disabled is True and stop_disabled is False
    assert captured["z_values"] == [2.0, 6.0]
    assert captured["ach_values"] == [3.0, 6.0]


def test_run_extend_action_sweep_requires_at_least_one_z_and_ach(tmp_path):
    _reset_run_states()
    guvcfd_app._pending_extend_modal.update(
        project_dir=str(tmp_path), settings={"sim-type": "decay"}, room=SimpleNamespace(x=1, y=1, z=1),
        status={"combos": {}}, action="sweep",
    )
    is_open, msg, tab, poll_disabled, run_disabled, stop_disabled = guvcfd_app._run_extend_action(
        1, None, None, None, None, None, "", "")
    assert "at least one Z value" in msg


def test_run_extend_action_extend_rejects_steady_state_projects(tmp_path):
    _reset_run_states()
    guvcfd_app._pending_extend_modal.update(
        project_dir=str(tmp_path), settings={"sim-type": "steady_state"}, room=SimpleNamespace(x=1, y=1, z=1),
        status={"combos": {"Z6_ACH3": {"z": 6.0, "ach": 3.0}}}, action="extend",
    )
    is_open, msg, tab, poll_disabled, run_disabled, stop_disabled = guvcfd_app._run_extend_action(
        1, None, None, None, ["Z6_ACH3"], 900, None, None)
    assert "only supported for decay" in msg


def test_run_extend_action_extend_starts_a_thread_with_the_selected_combos(tmp_path, monkeypatch):
    _reset_run_states()
    combos = {"Z6_ACH3": {"z": 6.0, "ach": 3.0}, "Z2_ACH3": {"z": 2.0, "ach": 3.0}}
    guvcfd_app._pending_extend_modal.update(
        project_dir=str(tmp_path), settings={"sim-type": "decay", "pimple-write-interval": 5},
        room=SimpleNamespace(x=1, y=1, z=1), status={"combos": combos}, action="extend",
    )

    captured = {}

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            captured["target"] = target
            captured["args"] = args

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(guvcfd_app.threading, "Thread", FakeThread)

    is_open, msg, tab, poll_disabled, run_disabled, stop_disabled = guvcfd_app._run_extend_action(
        1, None, None, None, ["Z6_ACH3"], 900, None, None)

    assert is_open is False and tab == "scenario-runs"
    assert poll_disabled is False and run_disabled is True and stop_disabled is False
    assert captured["started"] is True
    case_dirs, end_time, write_interval = captured["args"]
    assert end_time == 900 and write_interval == 5
    assert case_dirs == [(f"{tmp_path}/Z6_ACH3", 6.0, 3.0)]


def test_run_extend_action_extend_requires_a_selection_and_duration(tmp_path):
    _reset_run_states()
    guvcfd_app._pending_extend_modal.update(
        project_dir=str(tmp_path), settings={"sim-type": "decay"}, room=SimpleNamespace(x=1, y=1, z=1),
        status={"combos": {}}, action="extend",
    )
    result = guvcfd_app._run_extend_action(1, None, None, None, None, None, None, None)
    assert "Pick at least one combination" in result[1]

    result = guvcfd_app._run_extend_action(1, None, None, None, ["Z6_ACH3"], None, None, None)
    assert "Enter a new end time" in result[1]


# --- _extend_pipeline_thread ---

def test_extend_pipeline_thread_records_done_and_error_per_combo(monkeypatch):
    _reset_run_states()

    def fake_continue_decay(case_dir, end_time, write_interval):
        if "boom" in case_dir:
            raise RuntimeError("boom happened")

    monkeypatch.setattr(guvcfd_app, "_continue_decay", fake_continue_decay)

    case_dirs = [("/proj/Z6_ACH3", 6.0, 3.0), ("/proj/boom_ACH3", 2.0, 3.0)]
    guvcfd_app._extend_pipeline_thread(case_dirs, 900, 5)

    assert guvcfd_app._scenario_state["results"][(6.0, 3.0)] == {"status": "done", "detail": None}
    assert guvcfd_app._scenario_state["results"][(2.0, 3.0)]["status"] == "error"
    assert guvcfd_app._scenario_state["status"] == "done"


# --- cleanup action (two-step: prompt, then confirm) ---

def test_prompt_extend_cleanup_requires_a_loaded_project():
    _reset_run_states()
    displayed, message, msg = guvcfd_app._prompt_extend_cleanup(1)
    assert displayed is False
    assert msg == "Load a project folder first."


def test_prompt_extend_cleanup_reports_nothing_to_delete_when_no_dirs_exist(tmp_path):
    _reset_run_states()
    guvcfd_app._pending_extend_modal.update(project_dir=str(tmp_path), status={"ach_bases": {}})
    displayed, message, msg = guvcfd_app._prompt_extend_cleanup(1)
    assert displayed is False
    assert "nothing to delete" in msg


def test_prompt_extend_cleanup_names_the_exact_folders_and_stores_them(tmp_path):
    _reset_run_states()
    base_dir = tmp_path / "_base_ACH3"
    control_dir = tmp_path / "_control_ACH3"
    base_dir.mkdir()
    control_dir.mkdir()
    guvcfd_app._pending_extend_modal.update(
        project_dir=str(tmp_path),
        status={"ach_bases": {"3": {"base_dir": str(base_dir), "control_dir": str(control_dir)}}},
    )

    displayed, message, msg = guvcfd_app._prompt_extend_cleanup(1)

    assert displayed is True
    assert "_base_ACH3" in message and "_control_ACH3" in message
    assert "results.json are NOT affected" in message
    assert guvcfd_app._pending_extend_cleanup["dirs"] == [f"{tmp_path}/_base_ACH3", f"{tmp_path}/_control_ACH3"]


def test_confirm_extend_cleanup_removes_dirs_and_clears_ach_bases(tmp_path, monkeypatch):
    _reset_run_states()
    project_name = tmp_path.name
    base_dir = tmp_path / "_base_ACH3"
    control_dir = tmp_path / "_control_ACH3"
    base_dir.mkdir()
    control_dir.mkdir()
    update_ach_base_status(str(tmp_path), project_name, 3.0, "fp1", str(base_dir), str(control_dir), {})

    removed = []
    monkeypatch.setattr(guvcfd_app, "run_wsl_or_raise", lambda cmd, *a, **k: removed.append(cmd))
    monkeypatch.setattr(guvcfd_app, "wsl_path", lambda p: p)
    guvcfd_app._pending_extend_modal.update(project_dir=str(tmp_path), project_name=project_name)
    guvcfd_app._pending_extend_cleanup["dirs"] = [str(base_dir), str(control_dir)]

    body, msg = guvcfd_app._confirm_extend_cleanup(1)

    assert len(removed) == 1
    assert "_base_ACH3" in removed[0] and "_control_ACH3" in removed[0]
    assert "Deleted 2" in msg
    from guvcfd.project_status import load_project_status
    assert load_project_status(str(tmp_path), project_name)["ach_bases"] == {}
    assert guvcfd_app._pending_extend_cleanup["dirs"] is None  # cleared after use


def test_confirm_extend_cleanup_noop_when_nothing_pending(monkeypatch):
    _reset_run_states()
    removed = []
    monkeypatch.setattr(guvcfd_app, "run_wsl_or_raise", lambda cmd, *a, **k: removed.append(cmd))
    import dash
    body, msg = guvcfd_app._confirm_extend_cleanup(1)
    assert removed == []
    assert body is dash.no_update
