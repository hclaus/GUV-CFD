"""GUV-CFD GUI: load a .guv project, configure inlet/outlet/fan and the
scenario type, preview the 3D case setup live, and (eventually) run the
pipeline. Local single-user tool - run `python -m guvcfd.app` and open
the printed localhost URL.
"""
import dash
import dash_bootstrap_components as dbc
import plotly.graph_objs as go
from dash import Input, Output, State, dcc, html
from guv_calcs import Project

from .visualization import plot_case

# Single-user local tool - a plain module-level holder for the currently
# loaded project is simpler and more appropriate here than real session
# state (dcc.Store can't hold a Project object directly - not JSON-safe).
_loaded = {"project": None, "room": None, "path": None}

WALL_OPTIONS = [{"label": w, "value": w} for w in ("xMin", "xMax")]

app = dash.Dash(__name__, external_stylesheets=[dbc.themes.FLATLY])
app.title = "GUV-CFD"


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


def _opening_controls(prefix, default_wall, default_center, default_size):
    return [
        dbc.Checkbox(id=f"{prefix}-show", value=True, label="Show in preview", className="mb-2"),
        _labeled("Wall", dcc.Dropdown(id=f"{prefix}-wall", options=WALL_OPTIONS,
                                       value=default_wall, clearable=False)),
        _labeled(f"Center position (fraction of wall)",
                 html.Div([
                     dcc.Slider(id=f"{prefix}-center-y", min=0, max=1, step=0.01,
                                value=default_center[0], marks=None,
                                tooltip={"placement": "bottom", "always_visible": False}),
                     dcc.Slider(id=f"{prefix}-center-z", min=0, max=1, step=0.01,
                                value=default_center[1], marks=None,
                                tooltip={"placement": "bottom", "always_visible": False}),
                 ]),
                 help_text="top slider: across the wall - bottom slider: up the wall"),
        _labeled("Opening size, W x H (m)", dbc.Row([
            dbc.Col(dcc.Input(id=f"{prefix}-size-w", type="number", value=default_size[0],
                               min=0.05, max=2.0, step=0.05, className="form-control form-control-sm")),
            dbc.Col(dcc.Input(id=f"{prefix}-size-h", type="number", value=default_size[1],
                               min=0.05, max=2.0, step=0.05, className="form-control form-control-sm")),
        ])),
    ]


app.layout = dbc.Container([
    dbc.Row(dbc.Col(html.H4("GUV-CFD", className="mt-3 mb-1"))),
    dbc.Row(dbc.Col(html.Div(
        "guv-calcs UV fluence × OpenFOAM CFD — configure a case, preview it, then run.",
        className="text-muted small mb-3",
    ))),

    dbc.Row([
        # --- left column: inputs ---
        dbc.Col([
            _card("Project", [
                _labeled(".guv file path", dcc.Input(
                    id="guv-path", type="text", debounce=True,
                    placeholder=r"C:\path\to\room.guv", className="form-control form-control-sm",
                )),
                dbc.Button("Load", id="load-btn", size="sm", color="primary", className="mt-1"),
                html.Div(id="project-status", className="small text-muted mt-2"),
            ]),

            _card("Ventilation & UV", [
                _labeled("Air changes per hour (ACH)", dcc.Input(
                    id="ach", type="number", value=3.0, min=0.1, max=20, step=0.1,
                    className="form-control form-control-sm")),
                _labeled("Z — UV susceptibility (cm²/mJ)", dcc.Input(
                    id="z-value", type="number", value=2.0, min=0.01, max=20, step=0.1,
                    className="form-control form-control-sm")),
            ]),

            _card("Inlet", _opening_controls(
                "inlet", "xMin", (0.5, 0.85), (0.3, 0.3))),

            _card("Outlet", _opening_controls(
                "outlet", "xMax", (0.5, 0.15), (0.3, 0.3))),

            _card("Mixing fan", [
                dbc.Checkbox(id="fan-enable", value=False, label="Enable fan", className="mb-2"),
                html.Div(id="fan-controls", children=[
                    _labeled("Speed (m/s), 0.05–0.5 typical", dcc.Slider(
                        id="fan-speed", min=0.05, max=0.5, step=0.01, value=0.3,
                        marks={0.05: "0.05", 0.5: "0.5"},
                        tooltip={"placement": "bottom", "always_visible": True})),
                    _labeled("Radius (m)", dcc.Input(
                        id="fan-radius", type="number", value=0.6, min=0.1, max=1.5, step=0.05,
                        className="form-control form-control-sm")),
                    _labeled("Thickness (m)", dcc.Input(
                        id="fan-thickness", type="number", value=0.2, min=0.05, max=1.0, step=0.05,
                        className="form-control form-control-sm")),
                    _labeled("Height below ceiling (m)", dcc.Input(
                        id="fan-height-below-ceiling", type="number", value=0.3, min=0.0, max=2.0,
                        step=0.05, className="form-control form-control-sm")),
                ]),
            ]),

            _card("Simulation type", [
                dbc.RadioItems(
                    id="sim-type",
                    options=[
                        {"label": "Decay — one-time, room starts fully contaminated", "value": "decay"},
                        {"label": "Steady state — continuous source, before/after UV", "value": "steady_state"},
                    ],
                    value="decay", className="mb-2",
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
                        className="form-control form-control-sm")),
                    _labeled("Phase 1 iterations (no UV)", dcc.Input(
                        id="phase1-iterations", type="number", value=8000, min=500, max=50000, step=500,
                        className="form-control form-control-sm")),
                    _labeled("Phase 2 iterations (UV on)", dcc.Input(
                        id="phase2-iterations", type="number", value=3000, min=500, max=50000, step=500,
                        className="form-control form-control-sm")),
                ]),
            ]),

            dbc.Button("Run simulation", id="run-btn", color="success", className="w-100 mb-4", disabled=True),
            html.Div("Run wiring not built yet — preview only for now.",
                     className="small text-muted text-center mb-4"),
        ], width=4, style={"maxHeight": "92vh", "overflowY": "auto"}),

        # --- right column: 3D preview ---
        dbc.Col([
            dcc.Graph(id="preview-graph", style={"height": "88vh"},
                      figure=go.Figure(layout=dict(
                          annotations=[dict(text="Load a .guv project to preview the case",
                                             showarrow=False, font=dict(size=16, color="#888"))],
                      ))),
        ], width=8),
    ]),
], fluid=True)


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
    Input("load-btn", "n_clicks"),
    State("guv-path", "value"),
    prevent_initial_call=True,
)
def _load_project(n_clicks, path):
    if not path:
        return "Enter a .guv path first."
    try:
        project = Project.load(path)
        room = next(iter(project.rooms.values()))
    except Exception as e:
        return f"Failed to load: {e}"
    _loaded["project"] = project
    _loaded["room"] = room
    _loaded["path"] = path
    return f"Loaded: {room.x:.2f} x {room.y:.2f} x {room.z:.2f} {room.units}, {len(room.lamps)} lamp(s)"


@app.callback(
    Output("preview-graph", "figure"),
    Input("project-status", "children"),
    Input("inlet-show", "value"), Input("inlet-wall", "value"),
    Input("inlet-center-y", "value"), Input("inlet-center-z", "value"),
    Input("inlet-size-w", "value"), Input("inlet-size-h", "value"),
    Input("outlet-show", "value"), Input("outlet-wall", "value"),
    Input("outlet-center-y", "value"), Input("outlet-center-z", "value"),
    Input("outlet-size-w", "value"), Input("outlet-size-h", "value"),
    Input("fan-enable", "value"), Input("fan-speed", "value"),
    Input("fan-radius", "value"), Input("fan-thickness", "value"),
    Input("fan-height-below-ceiling", "value"),
)
def _update_preview(_status, inlet_show, inlet_wall, inlet_cy, inlet_cz, inlet_w, inlet_h,
                     outlet_show, outlet_wall, outlet_cy, outlet_cz, outlet_w, outlet_h,
                     fan_enable, fan_speed, fan_radius, fan_thickness, fan_height_below_ceiling):
    room = _loaded["room"]
    if room is None:
        return go.Figure(layout=dict(
            annotations=[dict(text="Load a .guv project to preview the case",
                               showarrow=False, font=dict(size=16, color="#888"))],
        ))

    fan_kwargs = {}
    if fan_enable:
        fan_kwargs = dict(
            fan_speed=fan_speed, fan_disk_radius=fan_radius, fan_disk_thickness=fan_thickness,
            fan_center=(room.x / 2, room.y / 2, room.z - fan_height_below_ceiling),
            fan_direction=(0, 0, -1),
        )

    fig = plot_case(
        room,
        inlet_wall=inlet_wall, inlet_center=(inlet_cy, inlet_cz), inlet_size=(inlet_w, inlet_h),
        outlet_wall=outlet_wall, outlet_center=(outlet_cy, outlet_cz), outlet_size=(outlet_w, outlet_h),
        title="", **fan_kwargs,
    )
    if not inlet_show:
        fig.data = [t for t in fig.data if not (t.customdata and str(t.customdata[0]).startswith("inlet"))]
    if not outlet_show:
        fig.data = [t for t in fig.data if not (t.customdata and str(t.customdata[0]).startswith("outlet"))]
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
    return fig


if __name__ == "__main__":
    app.run(debug=True)
