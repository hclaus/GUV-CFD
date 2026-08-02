"""Pure, framework-independent helpers - deliberately reimplemented here
rather than imported from guvcfd.app, to keep this app fully decoupled
from the Dash app while it's new/unproven (importing guvcfd.app would also
pull in the entire Dash object graph just for a handful of small
functions). Where a helper already exists as genuinely pure code
elsewhere (guvcfd.visualization.center_frac_for_wall, guvcfd.mesh_gen's
geometry functions, guvcfd.app_settings), it's imported directly instead
of duplicated.
"""
import math
import re
from pathlib import Path

from ..visualization import center_frac_for_wall
from ..wsl_utils import run_wsl

TEMPLATE_CASE_DIR = str(Path(__file__).resolve().parent.parent / "templates" / "case_template")

MONITOR_POINT_IDS = (1, 2, 3)

_UNSAFE_FOLDER_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_folder_name(name):
    name = _UNSAFE_FOLDER_CHARS_RE.sub("_", name).strip("_")
    return name or "case"


def parse_number_list(text):
    """"2, 6, 6.5" -> [2.0, 6.0, 6.5]; [] for empty/blank input. Raises
    ValueError (with the offending token) on anything that doesn't parse.
    """
    if not text or not text.strip():
        return []
    values = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(float(part))
        except ValueError:
            raise ValueError(f"'{part}' is not a number")
    return values


def compute_default_run_dir():
    """Ask WSL for OpenFOAM's own $FOAM_RUN and create it if missing - a
    real, usable default project directory rather than a guess. "" if WSL
    isn't reachable (caller should fall back to letting the user pick).
    """
    try:
        r = run_wsl('mkdir -p "$FOAM_RUN"; printf "%s|%s" "$WSL_DISTRO_NAME" "$FOAM_RUN"', "$HOME")
        distro, _, run_path = r.stdout.strip().partition("|")
        if not run_path:
            return ""
        return "\\\\wsl.localhost\\" + distro + run_path.replace("/", "\\")
    except Exception:
        return ""


def fresh_case_dir(guv_path, default_run_dir):
    """A new, never-colliding project-directory default under
    default_run_dir, named after the loaded .guv file."""
    if not default_run_dir:
        return default_run_dir
    base_name = sanitize_folder_name(Path(guv_path).stem if guv_path else "case")
    candidate = f"{default_run_dir}\\{base_name}"
    n = 2
    while Path(candidate).exists():
        candidate = f"{default_run_dir}\\{base_name}-{n}"
        n += 1
    return candidate


def opening_center_frac(settings, prefix, room):
    """(c1, c2) fractions for setup_case()'s inlet_center/outlet_center."""
    return center_frac_for_wall(settings[f"{prefix}-wall"], settings[f"{prefix}-y-input"],
                                 settings[f"{prefix}-z-input"], room)


def fan_kwargs(settings):
    if not settings.get("fan-enable"):
        return {}
    direction = (0, 0, -1) if settings.get("fan-direction", "down") == "down" else (0, 0, 1)
    return dict(
        fan_speed=settings["fan-speed"],
        fan_center=(settings["fan-x-input"], settings["fan-y-input"], settings["fan-z-input"]),
        fan_direction=direction,
        fan_disk_radius=settings["fan-radius"],
        fan_disk_thickness=settings["fan-thickness"],
    )


def second_opening_kwargs(settings, prefix, room):
    """setup_case()'s inlet2_*/outlet2_* kwargs for a 2nd inlet/outlet -
    {} when its own "Enable 2nd ..." toggle is off, matching setup_case()'s
    "no 2nd opening" default."""
    if not settings.get(f"{prefix}-enable"):
        return {}
    kwargs = {
        f"{prefix}_wall": settings[f"{prefix}-wall"],
        f"{prefix}_center": opening_center_frac(settings, prefix, room),
        f"{prefix}_size": (settings[f"{prefix}-size-w"], settings[f"{prefix}-size-h"]),
    }
    if prefix == "inlet2":  # only inlets have a diffuser type, not outlets
        kwargs["inlet2_diffuser_type"] = settings.get("inlet2-diffuser-type", "direct")
    return kwargs


def gather_monitoring_points(settings):
    """Enabled monitoring points from settings, in the shape
    monitoring_points.compute_monitoring_results() expects."""
    if not settings.get("monitoring-enable"):
        return []
    points = []
    for i in MONITOR_POINT_IDS:
        if not settings.get(f"monitor{i}-enable"):
            continue
        points.append({
            "name": settings.get(f"monitor{i}-name") or f"Point {i}",
            "x": settings[f"monitor{i}-x-input"],
            "y": settings[f"monitor{i}-y-input"],
            "z": settings[f"monitor{i}-z-input"],
            "cells_per_side": settings[f"monitor{i}-cells"],
        })
    return points


def settling_iterations(lambda_per_hr, target_fraction=0.995, min_iterations=500, max_iterations=50000):
    """Iterations (== seconds at deltaT=1s) to settle to target_fraction of
    steady state for a first-order well-mixed decay (t = ln(1/(1-f))/lambda).
    """
    if lambda_per_hr <= 0:
        return max_iterations
    lambda_per_s = lambda_per_hr / 3600.0
    t = math.log(1.0 / (1.0 - target_fraction)) / lambda_per_s
    return int(min(max_iterations, max(min_iterations, round(t))))


def decay_run_durations(ach, eACH_well_mixed_est, adv):
    """Adaptive pimpleFoam durations for decay mode's UV-on run and (if
    not sealed) UV-off control run - see app.py's identical helper for the
    full reasoning."""
    ceiling = 7200
    control_end_time = settling_iterations(
        ach, target_fraction=adv["decay-ach-min-fraction"] / 100.0, max_iterations=ceiling)

    combined_rate = ach + eACH_well_mixed_est
    raw_each_max_time = settling_iterations(
        combined_rate, target_fraction=adv["decay-each-max-fraction"] / 100.0, max_iterations=10 ** 9)
    each_target = (adv["decay-each-max-fraction"] if raw_each_max_time <= ceiling
                   else adv["decay-each-min-fraction"])
    combined_end_time = settling_iterations(combined_rate, target_fraction=each_target / 100.0,
                                             max_iterations=ceiling)
    return combined_end_time, control_end_time


def sealed_room_error(sim_type, ach, fan_enabled):
    """None if fine, else a user-facing message - see app._sealed_room_error
    (same rule: ACH<=0 is decay-only, and needs the mixing fan)."""
    if ach > 0:
        return None
    if sim_type != "decay":
        return ("Sealed-room / ACH<=0 is only supported in Decay mode - steady-state "
                "has no sensible zero-ventilation case.")
    if not fan_enabled:
        return ("Sealed room (ACH<=0) needs the mixing fan enabled - with no ventilation "
                "and no fan, there's no way for the flow field to develop.")
    return None
