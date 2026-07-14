"""3D preview of a case's OpenFOAM-relevant setup - room + lamps (via
guv_calcs.RoomPlotter, reused as-is: lamps render as their actual
photometric web mesh plus an aim-direction line, not just a placeholder
arrow) extended with inlet/outlet openings, an optional mixing fan, and
wall labels (so it's clear which wall is which without cross-referencing
the mesh dict).

Takes the same parameters as run_pipeline.setup_case() - meant for
previewing a configuration *before* running mesh generation/OpenFOAM, not
for reading back an already-built case.
"""
import numpy as np
import plotly.graph_objs as go
from guv_calcs.room_plotter import RoomPlotter

from .initial_fields import WALL_INFLOW_DIRECTION
from .mesh_gen import _opening_box, _WALL_SPECS

_WALL_LABEL_POSITIONS = {
    # wall name -> (position, room-fraction basis)
    "xMinWall": lambda Lx, Ly, Lz: (0, Ly / 2, Lz / 2),
    "xMaxWall": lambda Lx, Ly, Lz: (Lx, Ly / 2, Lz / 2),
    "frontWall": lambda Lx, Ly, Lz: (Lx / 2, 0, Lz / 2),
    "backWall": lambda Lx, Ly, Lz: (Lx / 2, Ly, Lz / 2),
    "floor": lambda Lx, Ly, Lz: (Lx / 2, Ly / 2, 0),
    "ceiling": lambda Lx, Ly, Lz: (Lx / 2, Ly / 2, Lz),
}


def _remove_zone_traces(fig):
    """Strip the calc-zone traces RoomPlotter adds automatically (Whole Room
    Fluence, Eye Dose, etc.) - not relevant to a CFD case-setup preview.
    Zone traces are tagged customdata=["zone_<id>"] by RoomPlotter itself.
    """
    fig.data = [t for t in fig.data if not (t.customdata and str(t.customdata[0]).startswith("zone_"))]
    return fig


def _add_wall_labels(fig, Lx, Ly, Lz):
    xs, ys, zs, texts = [], [], [], []
    for name, fn in _WALL_LABEL_POSITIONS.items():
        x, y, z = fn(Lx, Ly, Lz)
        xs.append(x)
        ys.append(y)
        zs.append(z)
        texts.append(name)
    fig.add_trace(go.Scatter3d(
        x=xs, y=ys, z=zs, mode="text", text=texts,
        textfont=dict(size=13, color="#444444"),
        name="Wall labels", customdata=["wall_labels"], showlegend=False,
    ))
    return fig


def _rect_outline(center, wall, size):
    """(x,y,z) outline of a rectangular opening on any of the 6 room walls -
    drawn in whichever two axes are actually in-plane for this wall (see
    mesh_gen._WALL_SPECS), not always (y,z)."""
    _, _, (a1, a2) = _WALL_SPECS[wall]
    w, h = size
    corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2), (-w / 2, -h / 2)]
    xs, ys, zs = [], [], []
    for d1, d2 in corners:
        p = list(center)
        p[a1] += d1
        p[a2] += d2
        xs.append(p[0])
        ys.append(p[1])
        zs.append(p[2])
    return xs, ys, zs


def _add_label(fig, position, text, color, name, customdata, size=10):
    x, y, z = position
    fig.add_trace(go.Scatter3d(
        x=[x], y=[y], z=[z], mode="text", text=[text],
        textfont=dict(size=size, color=color),
        name=name, customdata=[customdata], showlegend=False,
    ))
    return fig


def _add_opening(fig, label, wall, center_frac, size, Lx, Ly, Lz, color, flow_direction):
    """flow_direction: unit vector the arrow points along - the caller
    decides this (inlet: WALL_INFLOW_DIRECTION[wall], i.e. air entering;
    outlet: the negated inward normal, i.e. air leaving) rather than this
    function guessing intent from `label`, since now that openings can be
    on any of the 6 walls there's no single "always +X" convention that
    makes sense the way it did when only xMin/xMax were possible.
    """
    lo, hi = _opening_box(wall, Lx, Ly, Lz, center_frac, size, eps=0.0)
    center = tuple((a + b) / 2 for a, b in zip(lo, hi))
    x, y, z = _rect_outline(center, wall, size)
    fig.add_trace(go.Scatter3d(
        x=x, y=y, z=z, mode="lines", line=dict(color=color, width=5),
        name=label, customdata=[f"{label}_outline"], showlegend=True,
    ))
    arrow_len = min(Lx, Ly, Lz) * 0.15
    tip = tuple(c + d * arrow_len for c, d in zip(center, flow_direction))
    fig.add_trace(go.Scatter3d(
        x=[center[0], tip[0]], y=[center[1], tip[1]], z=[center[2], tip[2]],
        mode="lines+markers", line=dict(color=color, width=4),
        marker=dict(size=[0, 5], color=color, symbol="diamond"),
        name=label + " flow", customdata=[f"{label}_arrow"], showlegend=False,
    ))
    _, _, (a1, a2) = _WALL_SPECS[wall]
    label_pos = list(center)
    label_pos[a2] += size[1] / 2 + 0.1
    fig = _add_label(fig, tuple(label_pos), label, color, label, f"{label}_label")
    return fig


def _orthonormal_basis(direction):
    dx, dy, dz = direction
    mag = (dx ** 2 + dy ** 2 + dz ** 2) ** 0.5
    d = np.array([dx, dy, dz]) / mag
    arbitrary = (1, 0, 0) if abs(d[2]) > 0.9 else (0, 0, 1)
    u = np.cross(d, arbitrary)
    u = u / np.linalg.norm(u)
    v = np.cross(d, u)
    return d, u, v


def _cylinder_mesh(center, radius, thickness, direction, n_theta=32):
    """Vertex/face arrays (Mesh3d i/j/k triangle format) for a solid
    cylinder - the fan's actual swept volume (radius x thickness), not
    just a flat disk, so the preview matches the real cylinderToCell
    geometry (p1/p2 base/top centers + radius) used to carve its cellZone.
    """
    c = np.array(center, dtype=float)
    d, u, v = _orthonormal_basis(direction)

    theta = np.linspace(0, 2 * np.pi, n_theta, endpoint=False)
    ring = np.outer(np.cos(theta), u) + np.outer(np.sin(theta), v)  # (n_theta, 3)

    bottom_center = c - d * thickness / 2
    top_center = c + d * thickness / 2
    bottom_rim = bottom_center + radius * ring
    top_rim = top_center + radius * ring

    verts = np.vstack([bottom_center, top_center, bottom_rim, top_rim])
    BC, TC = 0, 1
    BR0, TR0 = 2, 2 + n_theta

    i_idx, j_idx, k_idx = [], [], []
    for a in range(n_theta):
        b = (a + 1) % n_theta
        i_idx += [BC, TC, BR0 + a, BR0 + b]
        j_idx += [BR0 + a, TR0 + b, BR0 + b, TR0 + b]
        k_idx += [BR0 + b, TR0 + a, TR0 + a, TR0 + a]

    return verts, np.array(i_idx), np.array(j_idx), np.array(k_idx), bottom_rim, top_rim


def _add_fan(fig, center, radius, direction, thickness=0.2, n_points=32, color="#e8a13a"):
    dx, dy, dz = direction
    mag = (dx ** 2 + dy ** 2 + dz ** 2) ** 0.5
    dx, dy, dz = dx / mag, dy / mag, dz / mag

    verts, i_idx, j_idx, k_idx, bottom_rim, top_rim = _cylinder_mesh(
        center, radius, thickness, direction, n_points,
    )
    fig.add_trace(go.Mesh3d(
        x=verts[:, 0], y=verts[:, 1], z=verts[:, 2],
        i=i_idx, j=j_idx, k=k_idx,
        color=color, opacity=0.3, flatshading=True,
        name="Fan", customdata=["fan_volume"], showlegend=True,
    ))
    # Crisp rim outlines at both faces, for definition against the transparent fill.
    for rim in (bottom_rim, top_rim):
        loop = np.vstack([rim, rim[:1]])
        fig.add_trace(go.Scatter3d(
            x=loop[:, 0], y=loop[:, 1], z=loop[:, 2], mode="lines",
            line=dict(color=color, width=4),
            name="Fan", customdata=["fan_rim"], showlegend=False,
        ))
    cx, cy, cz = center
    arrow_len = radius * 1.2
    tip = (cx + dx * arrow_len, cy + dy * arrow_len, cz + dz * arrow_len)
    fig.add_trace(go.Scatter3d(
        x=[cx, tip[0]], y=[cy, tip[1]], z=[cz, tip[2]],
        mode="lines+markers", line=dict(color=color, width=5),
        marker=dict(size=[0, 6], color=color, symbol="diamond"),
        name="Fan direction", customdata=["fan_arrow"], showlegend=False,
    ))
    fig = _add_label(fig, (cx, cy, cz + radius + 0.15), "fan", color, "Fan", "fan_label")
    return fig


def _box_mesh(center, size):
    """Vertex/face arrays (Mesh3d i/j/k triangle format) for an axis-aligned
    cube of the given side length, centered at `center`."""
    cx, cy, cz = center
    h = size / 2
    x = [cx - h, cx + h, cx + h, cx - h, cx - h, cx + h, cx + h, cx - h]
    y = [cy - h, cy - h, cy + h, cy + h, cy - h, cy - h, cy + h, cy + h]
    z = [cz - h, cz - h, cz - h, cz - h, cz + h, cz + h, cz + h, cz + h]
    i = [0, 0, 4, 4, 0, 0, 3, 3, 0, 0, 1, 1]
    j = [1, 2, 5, 6, 1, 5, 2, 6, 3, 7, 2, 6]
    k = [2, 3, 6, 7, 5, 4, 6, 7, 7, 4, 6, 5]
    return x, y, z, i, j, k


def _add_monitoring_point(fig, center, size, label, color="#ff1493"):
    x, y, z, i, j, k = _box_mesh(center, size)
    fig.add_trace(go.Mesh3d(
        x=x, y=y, z=z, i=i, j=j, k=k,
        color=color, opacity=0.35, flatshading=True,
        name="Monitoring point", customdata=[f"{label}_monitor_volume"], showlegend=True,
    ))
    fig = _add_label(fig, (center[0], center[1], center[2] + size / 2 + 0.1),
                      label, color, label, f"{label}_monitor_label")
    return fig


def _add_injection(fig, center, color="#9b59b6"):
    cx, cy, cz = center
    fig.add_trace(go.Scatter3d(
        x=[cx], y=[cy], z=[cz], mode="markers",
        marker=dict(size=8, color=color, symbol="circle"),
        name="Injection", customdata=["injection_marker"], showlegend=True,
    ))
    fig = _add_label(fig, (cx, cy, cz + 0.15), "injection", color, "Injection", "injection_label")
    return fig


def plot_case(room, inlet_wall="xMin", inlet_center=(0.5, 0.85), inlet_size=(0.3, 0.3),
              outlet_wall="xMax", outlet_center=(0.5, 0.15), outlet_size=(0.3, 0.3),
              inlet2_wall=None, inlet2_center=None, inlet2_size=None,
              outlet2_wall=None, outlet2_center=None, outlet2_size=None,
              fan_center=None, fan_disk_radius=None, fan_disk_thickness=0.2,
              fan_direction=(0, 0, -1), fan_speed=None,
              injection_center=None,
              monitoring_points=None, cell_size=0.1,
              title=""):
    """Build the full preview figure: room + lamps (RoomPlotter) + inlet/
    outlet (+ optional 2nd inlet/outlet) + optional fan + optional
    injection point + optional monitoring points + wall labels. Returns a
    plotly Figure - render with fig.show() or fig.write_html(path).

    inlet2_*/outlet2_*: an optional 2nd inlet/outlet, same shape as the
    primary one - None (the default) draws nothing extra.

    monitoring_points: optional list of monitoring_points.py-shaped point
    dicts (name/x/y/z/cells_per_side). Each is drawn as the same box its
    cells_per_side * cell_size actually gets carved into for real (see
    monitoring_points.monitoring_topo_set_dict) - not just a placeholder
    marker, so the preview shows the true averaging volume.

    Calc-zone traces RoomPlotter adds automatically (Whole Room Fluence
    etc.) are stripped - not relevant to a CFD case-setup preview.
    """
    fig = RoomPlotter(room).plotly(title=title)
    fig = _remove_zone_traces(fig)
    fig = _add_wall_labels(fig, room.x, room.y, room.z)
    fig = _add_opening(fig, "inlet", inlet_wall, inlet_center, inlet_size,
                        room.x, room.y, room.z, color="#2ecc71",
                        flow_direction=WALL_INFLOW_DIRECTION[inlet_wall])
    fig = _add_opening(fig, "outlet", outlet_wall, outlet_center, outlet_size,
                        room.x, room.y, room.z, color="#e74c3c",
                        flow_direction=tuple(-d for d in WALL_INFLOW_DIRECTION[outlet_wall]))
    if inlet2_wall is not None:
        fig = _add_opening(fig, "inlet2", inlet2_wall, inlet2_center, inlet2_size,
                            room.x, room.y, room.z, color="#2ecc71",
                            flow_direction=WALL_INFLOW_DIRECTION[inlet2_wall])
    if outlet2_wall is not None:
        fig = _add_opening(fig, "outlet2", outlet2_wall, outlet2_center, outlet2_size,
                            room.x, room.y, room.z, color="#e74c3c",
                            flow_direction=tuple(-d for d in WALL_INFLOW_DIRECTION[outlet2_wall]))
    if fan_speed is not None:
        center = fan_center or (room.x / 2, room.y / 2, room.z - 0.3)
        radius = fan_disk_radius or 0.6
        fig = _add_fan(fig, center, radius, fan_direction, thickness=fan_disk_thickness)
    if injection_center is not None:
        fig = _add_injection(fig, injection_center)
    for p in (monitoring_points or []):
        size = p["cells_per_side"] * cell_size
        fig = _add_monitoring_point(fig, (p["x"], p["y"], p["z"]), size, p.get("name") or "monitor")
    return fig
