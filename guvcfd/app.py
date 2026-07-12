"""GUV-CFD GUI: load a .guv project, configure inlet/outlet/fan and the
scenario type, preview the 3D case setup live, and (eventually) run the
pipeline. Local single-user tool - run `python -m guvcfd.app` and open
the printed localhost URL.
"""
import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

import dash
import dash_bootstrap_components as dbc
import plotly.graph_objs as go
from dash import Input, Output, State, dcc, html
from guv_calcs import Project

from .decay_analysis import write_results_summary
from .fan import fan_fvoptions_entry
from .initial_fields import compute_inlet_velocity
from .run_pipeline import setup_case
from .steady_state_pipeline import run_steady_state_scenario
from .visualization import plot_case
from .wsl_utils import run_wsl, run_wsl_or_raise, wsl_path

# Reference case setup_case() copies its static config (controlDict,
# fvSchemes, fvSolution, transportProperties, turbulenceProperties,
# volAverageDict) from - a previously verified-working pimpleFoam/
# scalarTransportFoam case, bundled into the package itself (not a local
# user path) so the app is portable across machines/checkouts.
TEMPLATE_CASE_DIR = str(Path(__file__).parent / "templates" / "case_template")

# Single-user local tool - a plain module-level holder for the currently
# loaded project is simpler and more appropriate here than real session
# state (dcc.Store can't hold a Project object directly - not JSON-safe).
# settings_path is the currently open/saved .guvcfd file (None if unsaved).
_loaded = {"project": None, "room": None, "path": None, "settings_path": None}

WALL_OPTIONS = [{"label": w, "value": w} for w in ("xMin", "xMax")]

# Every plain-value form field that a GUV-CFD project file (.guvcfd, JSON)
# saves/restores. Position fields use their "-input" id, not "-slider" -
# the slider is kept in sync from it (see _register_position_field), so
# only the number box needs to round-trip.
SETTINGS_FIELDS = [
    "project-description", "case-dir", "ach", "z-value",
    "inlet-show", "inlet-wall", "inlet-y-input", "inlet-z-input", "inlet-size-w", "inlet-size-h",
    "outlet-show", "outlet-wall", "outlet-y-input", "outlet-z-input", "outlet-size-w", "outlet-size-h",
    "fan-enable", "fan-speed", "fan-direction", "fan-radius", "fan-thickness",
    "fan-x-input", "fan-y-input", "fan-z-input",
    "sim-type", "pimple-end-time", "pimple-write-interval",
    "target-t-ss", "inject-x-input", "inject-y-input", "inject-z-input",
    "phase1-iterations", "phase2-iterations",
]

# Position-field spec: (prefix, label, room-dimension attr for the slider's
# max, default-value function of room, initial default/min/max/step used
# before any project is loaded). Shared by inlet/outlet, fan, and injection
# controls so their slider<->number sync + "reset to room" callbacks can be
# registered in one loop instead of duplicated per field.
POSITION_FIELDS = [
    ("inlet-y", "Across-wall position — Y (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("inlet-z", "Height — Z (m)", "z", lambda r: 0.85 * r.z, 2.1, 0, 5, 0.05),
    ("outlet-y", "Across-wall position — Y (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("outlet-z", "Height — Z (m)", "z", lambda r: 0.15 * r.z, 0.4, 0, 5, 0.05),
    ("fan-x", "X position (m)", "x", lambda r: r.x / 2, 2.0, 0, 10, 0.05),
    ("fan-y", "Y position (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("fan-z", "Height — Z (m)", "z", lambda r: max(r.z - 0.3, 0), 2.2, 0, 5, 0.05),
    ("inject-x", "X position (m)", "x", lambda r: r.x / 2, 2.0, 0, 10, 0.05),
    ("inject-y", "Y position (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("inject-z", "Height — Z (m)", "z", lambda r: min(1.5, r.z), 1.5, 0, 5, 0.05),
]
_POSITION_FIELD_BY_PREFIX = {f[0]: f for f in POSITION_FIELDS}


def _compute_default_run_dir():
    """Ask WSL for OpenFOAM's own $FOAM_RUN convention and create it if
    missing, so the GUI's default project directory is a real, usable path
    rather than a guess. Returns a \\\\wsl.localhost\\... UNC path (browsable
    from Windows); wsl_utils.wsl_path() converts it back for subprocess use.
    """
    try:
        r = run_wsl('mkdir -p "$FOAM_RUN"; printf "%s|%s" "$WSL_DISTRO_NAME" "$FOAM_RUN"', "~")
        distro, _, run_path = r.stdout.strip().partition("|")
        if not run_path:
            return ""
        return "\\\\wsl.localhost\\" + distro + run_path.replace("/", "\\")
    except Exception:
        return ""


_DEFAULT_RUN_DIR = _compute_default_run_dir()

# Background-thread run state - a real pipeline run takes minutes, far too
# long for a single Dash callback/HTTP request, so it runs in a daemon
# thread while a dcc.Interval polls this dict for the GUI to display.
_run_state = {"status": "idle", "log": [], "case_dir": None}


def _run_log(msg):
    _run_state["log"].append(str(msg))


def _fan_kwargs(settings):
    if not settings["fan-enable"]:
        return {}
    direction = (0, 0, -1) if settings["fan-direction"] == "down" else (0, 0, 1)
    return dict(
        fan_speed=settings["fan-speed"],
        fan_center=(settings["fan-x-input"], settings["fan-y-input"], settings["fan-z-input"]),
        fan_direction=direction,
        fan_disk_radius=settings["fan-radius"],
        fan_disk_thickness=settings["fan-thickness"],
    )


def _run_decay(guv_path, case_dir, room, settings):
    summary = setup_case(
        guv_path, case_dir, template_case_dir=TEMPLATE_CASE_DIR,
        Z=settings["z-value"], ach=settings["ach"],
        inlet_wall=settings["inlet-wall"],
        inlet_center=(settings["inlet-y-input"] / room.y, settings["inlet-z-input"] / room.z),
        inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
        outlet_wall=settings["outlet-wall"],
        outlet_center=(settings["outlet-y-input"] / room.y, settings["outlet-z-input"] / room.z),
        outlet_size=(settings["outlet-size-w"], settings["outlet-size-h"]),
        pimple_end_time=settings["pimple-end-time"],
        pimple_write_interval=settings["pimple-write-interval"],
        log_fn=_run_log,
        **_fan_kwargs(settings),
    )

    case_dir_wsl = wsl_path(case_dir)
    _run_log("Running pimpleFoam (this can take a while)...")
    r = run_wsl("rm -f log.pimpleFoam; pimpleFoam > log.pimpleFoam 2>&1", case_dir_wsl)
    tail = run_wsl("tail -30 log.pimpleFoam", case_dir_wsl).stdout
    _run_log(tail)
    if r.returncode != 0 or "FOAM FATAL" in tail or "Floating Point Exception" in tail:
        raise RuntimeError(f"pimpleFoam failed (exit {r.returncode}), see log above")

    _run_log("Running postProcess volAverage...")
    run_wsl_or_raise("postProcess -dict system/volAverageDict", case_dir_wsl, "postProcess volAverage")

    _run_log("Writing results summary...")
    results = write_results_summary(
        case_dir, f"{case_dir}/results.json", settings["ach"],
        summary["eACH_uv_well_mixed_mean"], extra={"n_lamps": summary["n_lamps"]},
    )
    _run_log(f"Done. eACH_uv effective={results['eACH_uv_effective']:.4g} /hr "
             f"(well-mixed={results['eACH_uv_well_mixed']:.4g} /hr)")


def _run_steady_state(guv_path, case_dir, room, settings):
    fan_kwargs = _fan_kwargs(settings)

    _run_log("=== Setting up mesh, flow field, and UV zones ===")
    setup_case(
        guv_path, case_dir, template_case_dir=TEMPLATE_CASE_DIR,
        Z=settings["z-value"], ach=settings["ach"],
        inlet_wall=settings["inlet-wall"],
        inlet_center=(settings["inlet-y-input"] / room.y, settings["inlet-z-input"] / room.z),
        inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
        outlet_wall=settings["outlet-wall"],
        outlet_center=(settings["outlet-y-input"] / room.y, settings["outlet-z-input"] / room.z),
        outlet_size=(settings["outlet-size-w"], settings["outlet-size-h"]),
        log_fn=_run_log,
        **fan_kwargs,
    )

    fan_entry = None
    if settings["fan-enable"]:
        fan_entry = fan_fvoptions_entry(settings["fan-speed"], direction=fan_kwargs["fan_direction"])

    inlet_area = settings["inlet-size-w"] * settings["inlet-size-h"]
    room_volume = room.x * room.y * room.z
    inflow_dir = (1, 0, 0) if settings["inlet-wall"] == "xMin" else (-1, 0, 0)
    v_mag = compute_inlet_velocity(settings["ach"], room_volume, inlet_area)
    inlet_velocity = tuple(v_mag * d for d in inflow_dir)

    _run_log("=== Running steady-state two-phase scenario ===")
    result = run_steady_state_scenario(
        case_dir, room.x, room.y, room.z, settings["ach"], settings["z-value"],
        source_center=(settings["inject-x-input"], settings["inject-y-input"], settings["inject-z-input"]),
        target_T_ss=settings["target-t-ss"],
        inlet_velocity=inlet_velocity,
        phase1_iterations=settings["phase1-iterations"],
        phase2_iterations=settings["phase2-iterations"],
        fan_entry=fan_entry,
        log_fn=_run_log,
    )
    with open(f"{case_dir}/results.json", "w") as f:
        json.dump(result, f, indent=2)
    _run_log(f"Done. Reduction={result['reduction_pct']:.1f}%, "
             f"eACH_uv={result['eACH_uv_steady_state']:.4g} /hr")


def _run_pipeline_thread(sim_type, guv_path, case_dir, room, settings):
    try:
        if sim_type == "decay":
            _run_decay(guv_path, case_dir, room, settings)
        else:
            _run_steady_state(guv_path, case_dir, room, settings)
        _run_state["status"] = "done"
    except Exception as e:
        _run_log(f"ERROR: {e}")
        _run_state["status"] = "error"


app = dash.Dash(__name__, external_stylesheets=[dbc.themes.FLATLY])
app.title = "GUV-CFD"


def _native_open_file(filetypes, title):
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(title=title, filetypes=filetypes)
    root.destroy()
    # Tk returns forward-slash paths on Windows even for UNC (\\wsl.localhost\...)
    # paths - normalize so downstream code doesn't have to handle both forms.
    return path.replace("/", "\\") if path else None


def _native_choose_dir(title, initialdir=None):
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    kwargs = {"title": title}
    if initialdir:
        kwargs["initialdir"] = initialdir
    path = filedialog.askdirectory(**kwargs)
    root.destroy()
    return path.replace("/", "\\") if path else None


def _native_save_file(title, defaultextension, filetypes):
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.asksaveasfilename(
        title=title, defaultextension=defaultextension, filetypes=filetypes,
    )
    root.destroy()
    return path or None


def _empty_preview_figure():
    return go.Figure(layout=dict(
        annotations=[dict(text="Load a .guv project to preview the case",
                           showarrow=False, font=dict(size=16, color="#888"))],
    ))


def _card(title, children):
    return dbc.Card(
        [dbc.CardHeader(title, className="fw-semibold small text-uppercase"),
         dbc.CardBody(children)],
        className="mb-3",
    )


def _labeled(label, component, help_text=None):
    children = [html.Label(label, className="form-label small mb-1"), component]
    if help_text:
        children.append(html.Div(help_text, className="form-text small"))
    return html.Div(children, className="mb-2")


def _position_field(prefix, label, default, minv, maxv, step):
    return _labeled(label, dbc.Row([
        dbc.Col(dcc.Slider(id=f"{prefix}-slider", min=minv, max=maxv, step=step, value=default,
                            marks=None, tooltip={"placement": "bottom", "always_visible": False}),
                width=8, className="pt-1"),
        dbc.Col(dcc.Input(id=f"{prefix}-input", type="number", value=default, min=minv, max=maxv,
                           step=step, className="form-control form-control-sm"), width=4),
    ], align="center", className="g-2"))


def _position_field_component(prefix):
    _, label, _dim, _default_fn, default, minv, maxv, step = _POSITION_FIELD_BY_PREFIX[prefix]
    return _position_field(prefix, label, default, minv, maxv, step)


def _opening_controls(prefix, default_wall):
    return [
        dbc.Checkbox(id=f"{prefix}-show", value=True, label="Show in preview", className="mb-2"),
        _labeled("Wall", dcc.Dropdown(id=f"{prefix}-wall", options=WALL_OPTIONS,
                                       value=default_wall, clearable=False)),
        _position_field_component(f"{prefix}-y"),
        _position_field_component(f"{prefix}-z"),
        _labeled("Opening size, W x H (m)", dbc.Row([
            dbc.Col(dcc.Input(id=f"{prefix}-size-w", type="number", value=0.3,
                               min=0.05, max=2.0, step=0.05, className="form-control form-control-sm")),
            dbc.Col(dcc.Input(id=f"{prefix}-size-h", type="number", value=0.3,
                               min=0.05, max=2.0, step=0.05, className="form-control form-control-sm")),
        ])),
    ]


def _fan_position_controls():
    return [_position_field_component(p) for p in ("fan-x", "fan-y", "fan-z")]


def _injection_position_controls():
    return [_position_field_component(p) for p in ("inject-x", "inject-y", "inject-z")]


project_setup_tab = dbc.Row([
    # --- left column: inputs ---
    dbc.Col([
        _card("Project", [
            dbc.Button("Load .guv file...", id="load-btn", color="primary",
                       size="sm", className="w-100"),
            html.Div(id="project-status", className="small text-muted mt-2"),
            _labeled("Description", dcc.Textarea(
                id="project-description", value="",
                style={"width": "100%", "height": "60px"},
                className="form-control form-control-sm")),
        ]),

        _card("OpenFOAM project directory", [
            _labeled("Project directory (WSL path)", dbc.Row([
                dbc.Col(dcc.Input(
                    id="case-dir", type="text", debounce=True, value=_DEFAULT_RUN_DIR,
                    placeholder=r"\\wsl.localhost\Ubuntu\home\...\run",
                    className="form-control form-control-sm"), width=8),
                dbc.Col(dbc.Button("Browse...", id="browse-case-dir-btn", size="sm",
                                   color="secondary", className="w-100"), width=4),
            ], className="g-2")),
        ]),

        _card("Ventilation & UV", [
            _labeled("Air changes per hour (ACH)", dcc.Input(
                id="ach", type="number", value=3.0, min=0.1, max=20, step=0.1,
                className="form-control form-control-sm")),
            _labeled("Z — UV susceptibility (cm²/mJ)", dcc.Input(
                id="z-value", type="number", value=2.0, min=0.01, max=20, step=0.1,
                className="form-control form-control-sm")),
        ]),

        _card("Inlet", _opening_controls("inlet", "xMin")),

        _card("Outlet", _opening_controls("outlet", "xMax")),

        _card("Mixing fan", [
            dbc.Checkbox(id="fan-enable", value=False, label="Enable fan", className="mb-2"),
            html.Div(id="fan-controls", children=[
                _labeled("Speed (m/s), 0.05–0.5 typical", dcc.Slider(
                    id="fan-speed", min=0.05, max=0.5, step=0.01, value=0.3,
                    marks={0.05: "0.05", 0.5: "0.5"},
                    tooltip={"placement": "bottom", "always_visible": True})),
                _labeled("Direction", dbc.RadioItems(
                    id="fan-direction",
                    className="btn-group w-100",
                    inputClassName="btn-check",
                    labelClassName="btn btn-outline-secondary btn-sm",
                    labelCheckedClassName="active",
                    options=[
                        {"label": "Downward", "value": "down"},
                        {"label": "Upward", "value": "up"},
                    ],
                    value="down",
                )),
                _labeled("Radius (m)", dcc.Input(
                    id="fan-radius", type="number", value=0.6, min=0.1, max=1.5, step=0.05,
                    className="form-control form-control-sm")),
                _labeled("Thickness (m)", dcc.Input(
                    id="fan-thickness", type="number", value=0.2, min=0.05, max=1.0, step=0.05,
                    className="form-control form-control-sm")),
                *_fan_position_controls(),
            ]),
        ]),

        _card("Simulation type", [
            dbc.RadioItems(
                id="sim-type",
                className="btn-group w-100 mb-2",
                inputClassName="btn-check",
                labelClassName="btn btn-outline-primary",
                labelCheckedClassName="active",
                options=[
                    {"label": "Decay", "value": "decay"},
                    {"label": "Steady state", "value": "steady_state"},
                ],
                value="decay",
            ),
            html.Div(id="decay-controls", children=[
                _labeled("Simulated duration (s)", dcc.Input(
                    id="pimple-end-time", type="number", value=120, min=10, max=7200, step=10,
                    className="form-control form-control-sm")),
                _labeled("Write interval (s)", dcc.Input(
                    id="pimple-write-interval", type="number", value=10, min=1, max=600, step=1,
                    className="form-control form-control-sm")),
            ]),
            html.Div(id="steady-state-controls", children=[
                _labeled("Target well-mixed steady-state T", dcc.Input(
                    id="target-t-ss", type="number", value=0.3, min=0.01, max=1.0, step=0.01,
                    className="form-control form-control-sm"),
                    help_text="Injection flow (source strength) is calculated automatically "
                              "from this target and the ACH above."),
                html.Div("Injection position", className="small fw-semibold text-uppercase mt-3 mb-1"),
                *_injection_position_controls(),
                _labeled("Phase 1 iterations (no UV)", dcc.Input(
                    id="phase1-iterations", type="number", value=8000, min=500, max=50000, step=500,
                    className="form-control form-control-sm")),
                _labeled("Phase 2 iterations (UV on)", dcc.Input(
                    id="phase2-iterations", type="number", value=3000, min=500, max=50000, step=500,
                    className="form-control form-control-sm")),
            ]),
        ]),

        dbc.Button("Run simulation", id="run-btn", color="success", className="w-100 mb-2"),
        html.Div(id="run-status-text", className="small fw-semibold text-center mb-1"),
        html.Pre(id="run-log", className="small mb-4", style={
            "maxHeight": "220px", "overflowY": "auto", "fontSize": "11px",
            "background": "rgba(127,127,127,0.08)", "padding": "8px",
            "border": "1px solid rgba(127,127,127,0.3)", "whiteSpace": "pre-wrap",
        }),
        dcc.Interval(id="run-poll", interval=2000, n_intervals=0, disabled=True),
    ], width=4, style={"maxHeight": "88vh", "overflowY": "auto"}),

    # --- right column: 3D preview ---
    dbc.Col([
        dcc.Graph(id="preview-graph", style={"height": "88vh"}, figure=_empty_preview_figure()),
    ], width=8),
])

analysis_tab = html.Div(
    "Results will appear here once a simulation has been run.",
    className="text-muted p-5 text-center",
)

app.layout = dbc.Container([
    dcc.Store(id="fresh-room-load"),
    dbc.Row([
        dbc.Col(html.H4("GUV-CFD", className="mt-3 mb-1"), width="auto"),
        dbc.Col(dbc.DropdownMenu(
            label="File", color="light", size="sm", className="mt-3",
            children=[
                dbc.DropdownMenuItem("Open Project...", id="menu-open"),
                dbc.DropdownMenuItem("Save Project", id="menu-save"),
                dbc.DropdownMenuItem("Save Project As...", id="menu-save-as"),
            ],
        ), width="auto"),
        dbc.Col(html.Div("Untitled project", id="project-name-display",
                          className="mt-3 text-muted fst-italic"), width="auto"),
    ], align="center", className="g-3"),
    dbc.Row(dbc.Col(html.Div(
        "guv-calcs UV fluence × OpenFOAM CFD — configure a case, preview it, then run.",
        className="text-muted small mb-3",
    ))),
    dbc.Tabs([
        dbc.Tab(project_setup_tab, label="Project Setup", tab_id="project-setup"),
        dbc.Tab(analysis_tab, label="Analysis of Results", tab_id="analysis"),
    ], active_tab="project-setup", className="mb-3"),
], fluid=True)


# --- two-way slider<->number sync + reset-to-room-dimensions on load,
# one callback per position field (registered in a loop). ---
def _register_position_field(prefix, dim, default_fn):
    @app.callback(
        Output(f"{prefix}-slider", "value"),
        Output(f"{prefix}-input", "value"),
        Output(f"{prefix}-slider", "max"),
        Output(f"{prefix}-input", "max"),
        Input(f"{prefix}-slider", "value"),
        Input(f"{prefix}-input", "value"),
        Input("fresh-room-load", "data"),
        prevent_initial_call=True,
    )
    def _sync(slider_val, input_val, _fresh_load):
        # Only "Load .guv file..." (a genuinely new room, no saved positions
        # to restore) fires fresh-room-load - "Open Project" restores exact
        # saved values itself and updates max directly, bypassing this reset.
        trig = dash.ctx.triggered_id
        if trig == "fresh-room-load":
            room = _loaded["room"]
            if room is None:
                return dash.no_update, dash.no_update, dash.no_update, dash.no_update
            dim_size = round(getattr(room, dim), 3)
            default = round(default_fn(room), 3)
            return default, default, dim_size, dim_size
        if trig == f"{prefix}-slider":
            return dash.no_update, slider_val, dash.no_update, dash.no_update
        return input_val, dash.no_update, dash.no_update, dash.no_update

    _sync.__name__ = f"_sync_{prefix.replace('-', '_')}"


for _prefix, _label, _dim, _default_fn, *_rest in POSITION_FIELDS:
    _register_position_field(_prefix, _dim, _default_fn)


@app.callback(
    Output("decay-controls", "style"),
    Output("steady-state-controls", "style"),
    Input("sim-type", "value"),
)
def _toggle_sim_type_controls(sim_type):
    if sim_type == "decay":
        return {"display": "block"}, {"display": "none"}
    return {"display": "none"}, {"display": "block"}


@app.callback(
    Output("fan-controls", "style"),
    Input("fan-enable", "value"),
)
def _toggle_fan_controls(enabled):
    return {"display": "block"} if enabled else {"display": "none", "opacity": "0.4"}


@app.callback(
    Output("project-status", "children"),
    Output("project-description", "value"),
    Output("fresh-room-load", "data"),
    Input("load-btn", "n_clicks"),
    prevent_initial_call=True,
)
def _load_project(n_clicks):
    path = _native_open_file(
        [("GUV project files", "*.guv"), ("All files", "*.*")],
        "Select a .guv project file",
    )
    if not path:
        return dash.no_update, dash.no_update, dash.no_update
    try:
        project = Project.load(path)
        room = next(iter(project.rooms.values()))
    except Exception as e:
        return f"Failed to load: {e}", dash.no_update, dash.no_update
    _loaded["project"] = project
    _loaded["room"] = room
    _loaded["path"] = path
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    status = f"Loaded {name}: {room.x:.2f} x {room.y:.2f} x {room.z:.2f} {room.units}, {len(room.lamps)} lamp(s)"
    description = f"{room.x:.2f} x {room.y:.2f} x {room.z:.2f} {room.units} room"
    return status, description, n_clicks


@app.callback(
    Output("case-dir", "value"),
    Input("browse-case-dir-btn", "n_clicks"),
    State("case-dir", "value"),
    prevent_initial_call=True,
)
def _browse_case_dir(n_clicks, current_dir):
    path = _native_choose_dir("Select or create an OpenFOAM project directory",
                               initialdir=current_dir)
    if not path:
        return dash.no_update
    return path


@app.callback(
    Output("project-name-display", "children"),
    Input("menu-save", "n_clicks"),
    Input("menu-save-as", "n_clicks"),
    [State(fid, "value") for fid in SETTINGS_FIELDS],
    prevent_initial_call=True,
)
def _save_project(n_save, n_save_as, *values):
    trig = dash.ctx.triggered_id
    settings = dict(zip(SETTINGS_FIELDS, values))
    settings["guv_path"] = _loaded.get("path")

    path = _loaded.get("settings_path")
    if trig == "menu-save-as" or not path:
        path = _native_save_file(
            "Save GUV-CFD project",
            ".guvcfd",
            [("GUV-CFD project files", "*.guvcfd"), ("All files", "*.*")],
        )
        if not path:
            return dash.no_update

    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
    _loaded["settings_path"] = path
    return path.replace("\\", "/").rsplit("/", 1)[-1]


_open_outputs = [
    Output("project-name-display", "children", allow_duplicate=True),
    Output("project-status", "children", allow_duplicate=True),
]
_open_outputs += [Output(fid, "value", allow_duplicate=True) for fid in SETTINGS_FIELDS]
for _prefix, *_ in POSITION_FIELDS:
    _open_outputs.append(Output(f"{_prefix}-slider", "max", allow_duplicate=True))
    _open_outputs.append(Output(f"{_prefix}-input", "max", allow_duplicate=True))


@app.callback(
    *_open_outputs,
    Input("menu-open", "n_clicks"),
    prevent_initial_call=True,
)
def _open_project(n_clicks):
    n_outputs = len(_open_outputs)
    no_change = tuple(dash.no_update for _ in range(n_outputs))

    path = _native_open_file(
        [("GUV-CFD project files", "*.guvcfd"), ("All files", "*.*")],
        "Open a GUV-CFD project",
    )
    if not path:
        return no_change

    try:
        with open(path) as f:
            settings = json.load(f)
    except Exception as e:
        result = list(no_change)
        result[1] = f"Failed to open project: {e}"
        return tuple(result)

    guv_path = settings.get("guv_path")
    status = "No .guv file recorded in this project."
    room = None
    if guv_path:
        try:
            project = Project.load(guv_path)
            room = next(iter(project.rooms.values()))
            _loaded["project"] = project
            _loaded["room"] = room
            _loaded["path"] = guv_path
            gname = guv_path.replace("\\", "/").rsplit("/", 1)[-1]
            status = (f"Loaded {gname}: {room.x:.2f} x {room.y:.2f} x {room.z:.2f} "
                      f"{room.units}, {len(room.lamps)} lamp(s)")
        except Exception as e:
            status = f"Failed to reload {guv_path}: {e}"

    _loaded["settings_path"] = path
    proj_name = path.replace("\\", "/").rsplit("/", 1)[-1]

    field_values = [settings.get(fid) for fid in SETTINGS_FIELDS]
    max_values = []
    for _prefix, _label, dim, _default_fn, *_rest in POSITION_FIELDS:
        if room is not None:
            dim_size = round(getattr(room, dim), 3)
            max_values += [dim_size, dim_size]
        else:
            max_values += [dash.no_update, dash.no_update]

    return tuple([proj_name, status] + field_values + max_values)


@app.callback(
    Output("run-btn", "disabled"),
    Output("run-poll", "disabled", allow_duplicate=True),
    Output("run-log", "children", allow_duplicate=True),
    Input("run-btn", "n_clicks"),
    [State(fid, "value") for fid in SETTINGS_FIELDS],
    prevent_initial_call=True,
)
def _start_run(n_clicks, *values):
    if _run_state["status"] == "running":
        return True, False, dash.no_update

    room = _loaded["room"]
    guv_path = _loaded["path"]
    if room is None or guv_path is None:
        return False, True, "No .guv project loaded - use File > Open Project or Load .guv file first."

    settings = dict(zip(SETTINGS_FIELDS, values))
    case_dir = settings["case-dir"]
    if not case_dir:
        return False, True, "Set an OpenFOAM project directory first."

    _run_state["status"] = "running"
    _run_state["log"] = []
    _run_state["case_dir"] = case_dir

    thread = threading.Thread(
        target=_run_pipeline_thread,
        args=(settings["sim-type"], guv_path, case_dir, room, settings),
        daemon=True,
    )
    thread.start()
    return True, False, "Starting..."


@app.callback(
    Output("run-log", "children"),
    Output("run-status-text", "children"),
    Output("run-btn", "disabled", allow_duplicate=True),
    Output("run-poll", "disabled"),
    Input("run-poll", "n_intervals"),
    prevent_initial_call=True,
)
def _poll_run(n_intervals):
    status = _run_state["status"]
    log_text = "\n".join(_run_state["log"][-300:])
    status_text = {
        "running": "Running...",
        "done": "Finished.",
        "error": "Failed - see log below.",
    }.get(status, "")
    still_running = status == "running"
    return log_text, status_text, still_running, not still_running


@app.callback(
    Output("preview-graph", "figure"),
    Input("project-status", "children"),
    Input("inlet-show", "value"), Input("inlet-wall", "value"),
    Input("inlet-y-input", "value"), Input("inlet-z-input", "value"),
    Input("inlet-size-w", "value"), Input("inlet-size-h", "value"),
    Input("outlet-show", "value"), Input("outlet-wall", "value"),
    Input("outlet-y-input", "value"), Input("outlet-z-input", "value"),
    Input("outlet-size-w", "value"), Input("outlet-size-h", "value"),
    Input("fan-enable", "value"), Input("fan-speed", "value"), Input("fan-direction", "value"),
    Input("fan-radius", "value"), Input("fan-thickness", "value"),
    Input("fan-x-input", "value"), Input("fan-y-input", "value"), Input("fan-z-input", "value"),
    Input("sim-type", "value"),
    Input("inject-x-input", "value"), Input("inject-y-input", "value"), Input("inject-z-input", "value"),
)
def _update_preview(_status, inlet_show, inlet_wall, inlet_y, inlet_z, inlet_w, inlet_h,
                     outlet_show, outlet_wall, outlet_y, outlet_z, outlet_w, outlet_h,
                     fan_enable, fan_speed, fan_direction, fan_radius, fan_thickness,
                     fan_x, fan_y, fan_z, sim_type, inject_x, inject_y, inject_z):
    room = _loaded["room"]
    if room is None:
        return _empty_preview_figure()

    inlet_center = (inlet_y / room.y, inlet_z / room.z)
    outlet_center = (outlet_y / room.y, outlet_z / room.z)

    fan_kwargs = {}
    if fan_enable:
        direction = (0, 0, -1) if fan_direction == "down" else (0, 0, 1)
        fan_kwargs = dict(
            fan_speed=fan_speed, fan_disk_radius=fan_radius, fan_disk_thickness=fan_thickness,
            fan_center=(fan_x, fan_y, fan_z), fan_direction=direction,
        )

    injection_center = (inject_x, inject_y, inject_z) if sim_type == "steady_state" else None

    fig = plot_case(
        room,
        inlet_wall=inlet_wall, inlet_center=inlet_center, inlet_size=(inlet_w, inlet_h),
        outlet_wall=outlet_wall, outlet_center=outlet_center, outlet_size=(outlet_w, outlet_h),
        injection_center=injection_center,
        title="", **fan_kwargs,
    )
    if not inlet_show:
        fig.data = [t for t in fig.data if not (t.customdata and str(t.customdata[0]).startswith("inlet"))]
    if not outlet_show:
        fig.data = [t for t in fig.data if not (t.customdata and str(t.customdata[0]).startswith("outlet"))]
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
    return fig


if __name__ == "__main__":
    # use_reloader=False: Werkzeug's reloader re-execs this module in a
    # subprocess, which crashes here (likely the tkinter import or the
    # WSL subprocess call in _compute_default_run_dir() re-running in the
    # forked child) - verified by reproducing with/without it.
    app.run(debug=True, use_reloader=False)
