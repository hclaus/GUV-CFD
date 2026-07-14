"""Launch ParaView pointed at a case directory with a useful default view
already set up, instead of a blank scene the user has to configure by hand
every time: a log-scale volume render of T (contamination) in one view, a
Stream Tracer seeded broadly through the room (colored by U) in a second
(tiled) view showing the general circulation pattern, and - for steady-
state runs, where a source_center is known - a third view, in its own
separate layout tab, of streamlines seeded at the contaminant source
itself, colored by T, showing where contaminant-carrying air actually goes.

The room-wide seed (rather than seeding tightly at the inlet opening) is
deliberate: a small seed cloud right at the inlet just retraces the jet
core already visible in the T volume render and says nothing about the
room's broader circulation (recirculation zones, dead spots) - verified
directly, it produced a single redundant bundle of near-identical lines.

Finding the real ParaView install rather than assuming it's on PATH -
Windows installs it under Program Files and does not add it to PATH by
default.
"""
import subprocess
import tempfile
from pathlib import Path

_PARAVIEW_SEARCH_ROOTS = (r"C:\Program Files", r"C:\Program Files (x86)")


def find_paraview_exe():
    """Locate the newest installed ParaView's paraview.exe, or None."""
    candidates = []
    for root in _PARAVIEW_SEARCH_ROOTS:
        candidates.extend(Path(root).glob("ParaView*/bin/paraview.exe"))
    if not candidates:
        return None
    return str(sorted(candidates)[-1])


_SCRIPT_TEMPLATE = '''
import traceback

_log = open(r"{log_path}", "w")
def _log_step(msg):
    _log.write(msg + "\\n")
    _log.flush()

try:
    from paraview.simple import *
    _log_step("imported paraview.simple")

    reader = OpenFOAMReader(FileName=r"{case_foam}")
    # Refresh available timesteps/arrays from disk *before* selecting which
    # to load - ParaView otherwise caches the time-directory list as of
    # whenever the reader was first constructed, and never re-scans the
    # filesystem for time steps written later (e.g. by a "Continue" run
    # that extended an already-open case).
    reader.UpdatePipelineInformation()
    reader.CellArrays = ['T', 'U', 'p']
    reader.Createcelltopointfiltereddata = 1
    reader.UpdatePipeline()
    _log_step(f"reader loaded, {{len(reader.TimestepValues)}} timesteps available")

    # Constructing a reader via paraview.simple directly (as opposed to the
    # GUI's own File > Open, which sets this automatically) leaves the
    # animation scene's PlayMode at the generic default ('Sequence' with
    # NumberOfFrames=1) even though TimeKeeper.TimestepValues already has
    # every real timestep - verified directly (scene.NumberOfFrames == 1
    # right after Show(), despite tk.TimestepValues having all 100). With
    # only 1 frame across the whole time range, the GUI's time slider has
    # nothing meaningful to step through. 'Snap To TimeSteps' makes the
    # scene follow the reader's own discrete timesteps directly, which is
    # what OpenFOAM's stepped time-directory data actually is.
    scene = GetAnimationScene()
    scene.PlayMode = 'Snap To TimeSteps'
    tk = GetTimeKeeper()
    _log_step(f"animation scene PlayMode=Snap To TimeSteps, "
              f"{{len(tk.TimestepValues)}} timesteps in scene, "
              f"range [{{scene.StartTime}}, {{scene.EndTime}}]")

    view1 = GetActiveViewOrCreate('RenderView')
    view1.ViewSize = [900, 700]
    disp1 = Show(reader, view1)
    disp1.SetRepresentationType('Volume')
    ColorBy(disp1, ('POINTS', 'T'))
    # RescaleTransferFunctionToDataRange(True) only looks at whichever single
    # timestep happens to be current - verified directly (a full-window
    # screenshot at t=370 showed a flat, featureless blue blob, because the
    # color range was fixed from t=10's data and T had long since decayed
    # below it). *OverTime() scans every timestep once and fixes the range to
    # the true min/max across the whole run instead, so "red" means the same
    # absolute concentration throughout and the room visibly fades toward the
    # low end as T decays - the actual point of animating this field.
    disp1.RescaleTransferFunctionToDataRangeOverTime()
    # RescaleTransferFunctionToDataRangeOverTime() steps through every
    # timestep internally and leaves the scene sitting at whatever time it
    # finished on - verified directly (Time was left at 0, which isn't one
    # of the reader's actual timesteps since the first real one is 10) -
    # showing a blank volume view, since there's no data at an invalid time.
    # Explicitly return to the first real timestep afterward.
    scene.GoToFirst()

    # T is a concentration field with a source (or initial condition) many
    # orders of magnitude above the rest of the room - a linear color scale
    # makes almost the entire domain render as one flat "low" color even
    # though there's a real, continuous gradient, making the room look
    # emptier/less-mixed than it actually is. Log scale shows that gradient.
    # MapControlPointsToLogSpace() requires a strictly-positive range - T can
    # be exactly 0 in cells contaminant hasn't reached yet, so the true data
    # minimum isn't usable; floor it to a small fraction of the max instead.
    ctfT = GetColorTransferFunction('T')
    data_min, data_max = ctfT.RGBPoints[0], ctfT.RGBPoints[-4]
    floor = data_max * 1e-3 if data_max > 0 else 1e-6
    log_min = max(data_min, floor) if data_min > 0 else floor
    ctfT.RescaleTransferFunction(log_min, data_max)
    ctfT.MapControlPointsToLogSpace()
    ctfT.UseLogScale = 1
    _log_step(f"T color map set to log scale, range [{{log_min}}, {{data_max}}] "
              f"(true data min was {{data_min}})")

    ResetCamera(view1)
    _log_step(f"view1 (log-scale volume T) shown, scene time reset to {{scene.AnimationTime}}")

    streamTracer = StreamTracer(Input=reader, SeedType='Point Cloud')
    streamTracer.Vectors = ['POINTS', 'U']
    streamTracer.SeedType.Center = [{room_center_x}, {room_center_y}, {room_center_z}]
    streamTracer.SeedType.Radius = {room_seed_radius}
    streamTracer.SeedType.NumberOfPoints = 200
    streamTracer.MaximumStreamlineLength = {max_length}
    streamTracer.UpdatePipeline()
    _log_step("room-seeded stream tracer computed")

    try:
        layout1 = GetLayout(view1)
        if layout1 is None:
            layout1 = GetLayout()
        _log_step(f"layout1={{layout1}}")
        layout1.SplitHorizontal(0, 0.5)
        view2 = CreateRenderView()
        layout1.AssignView(2, view2)
        _log_step("split view2 into layout")
    except Exception:
        _log_step("layout split failed, falling back to a separate view:\\n" + traceback.format_exc())
        layout1 = None
        view2 = CreateRenderView()

    view2.ViewSize = [900, 700]
    disp2 = Show(streamTracer, view2)
    ColorBy(disp2, ('POINTS', 'U'))
    disp2.RescaleTransferFunctionToDataRange(True)
    Show(reader, view2).Opacity = 0.08
    ResetCamera(view2)
    _log_step("view2 (room-seeded stream tracer, colored by U) shown")
{view3_lines}
    RenderAllViews()
    _log_step("rendered")
    {screenshot_lines}
    _log_step("DONE-OK")
except Exception:
    _log_step("FAILED:\\n" + traceback.format_exc())
finally:
    _log.close()
'''

_VIEW3_TEMPLATE = '''
    sourceStreamTracer = StreamTracer(Input=reader, SeedType='Point Cloud')
    sourceStreamTracer.Vectors = ['POINTS', 'U']
    sourceStreamTracer.SeedType.Center = [{source_x}, {source_y}, {source_z}]
    sourceStreamTracer.SeedType.Radius = {source_seed_radius}
    sourceStreamTracer.SeedType.NumberOfPoints = 80
    sourceStreamTracer.MaximumStreamlineLength = {max_length}
    sourceStreamTracer.UpdatePipeline()
    _log_step("source-seeded stream tracer computed")

    # A second nested split of an already-split layout (e.g.
    # layout1.SplitVertical(2, 0.5) then AssignView(4, view3)) depends on
    # ParaView's internal cell-numbering scheme after that first split,
    # which isn't consistent across versions - verified directly: it ran
    # with no exception, but the new cell stayed empty (view3 got attached
    # to the wrong slot). A brand new layout tab sidesteps that guesswork
    # entirely - CreateRenderView() always attaches to the current
    # (freshly created, still-empty) layout on its own.
    try:
        CreateLayout('Source View (T)')
        view3 = CreateRenderView()
        _log_step("created separate 'Source View (T)' layout tab for view3")
    except Exception:
        _log_step("creating layout tab for view3 failed, falling back to a bare view:\\n"
                   + traceback.format_exc())
        view3 = CreateRenderView()

    view3.ViewSize = [900, 700]
    disp3 = Show(sourceStreamTracer, view3)
    # Colored by T using the *same* transfer function view1 already put in
    # log scale (ParaView shares one color map per field name by default) -
    # deliberately not rescaled again here, which would silently narrow that
    # shared map to just the streamline-sampled range and undo view1's
    # carefully-set full-domain log range.
    ColorBy(disp3, ('POINTS', 'T'))
    Show(reader, view3).Opacity = 0.08
    ResetCamera(view3)
    _log_step("view3 (source-seeded stream tracer, colored by T) shown")
'''


def _screenshot_lines(view1_png, view2_png, view3_png=None):
    if not view1_png and not view2_png and not view3_png:
        return ""
    lines = []
    if view1_png:
        lines.append(f'SaveScreenshot(r"{view1_png}", view1, ImageResolution=[900, 700])')
    if view2_png:
        lines.append(f'SaveScreenshot(r"{view2_png}", view2, ImageResolution=[900, 700])')
    if view3_png:
        lines.append(f'SaveScreenshot(r"{view3_png}", view3, ImageResolution=[900, 700])')
    return "\n    ".join(lines)


def build_preset_script(case_dir, mesh_bounds, log_path, screenshot_paths=(None, None, None),
                         source_center=None, source_seed_radius=0.1):
    """mesh_bounds: (xmin, xmax, ymin, ymax, zmin, zmax) - used to seed the
    room-wide stream tracer (a sphere centered on the room, sized to
    circumscribe its full bounding box - seeds landing outside the actual
    mesh just don't produce a visible line, harmless). log_path: where the
    script writes its own step-by-step progress/traceback - launched as a
    detached GUI process, so stdout/stderr redirection from the launcher
    side isn't reliable.

    source_center: (x, y, z) of the steady-state contaminant source, if
    known - adds a third view of streamlines seeded there, colored by T, so
    it's clear where contaminant-carrying air actually goes. None for
    decay-scenario cases, which have no continuous point source.
    """
    xmin, xmax, ymin, ymax, zmin, zmax = mesh_bounds
    max_length = (xmax - xmin) * 3
    room_center = ((xmin + xmax) / 2, (ymin + ymax) / 2, (zmin + zmax) / 2)
    dx, dy, dz = xmax - xmin, ymax - ymin, zmax - zmin
    room_seed_radius = ((dx ** 2 + dy ** 2 + dz ** 2) ** 0.5) / 2

    view3_lines = ""
    if source_center is not None:
        view3_lines = _VIEW3_TEMPLATE.format(
            source_x=source_center[0], source_y=source_center[1], source_z=source_center[2],
            source_seed_radius=source_seed_radius, max_length=max_length,
        )

    return _SCRIPT_TEMPLATE.format(
        case_foam=f"{case_dir}/case.foam",
        room_center_x=room_center[0], room_center_y=room_center[1], room_center_z=room_center[2],
        room_seed_radius=room_seed_radius, max_length=max_length,
        log_path=log_path,
        view3_lines=view3_lines,
        screenshot_lines=_screenshot_lines(*screenshot_paths),
    )


def launch_paraview(case_dir, mesh_bounds, source_center=None):
    """Write the preset script to a temp file and launch paraview.exe with
    it via --script - non-blocking (ParaView is its own GUI application).
    Raises FileNotFoundError if ParaView isn't installed. Returns
    (script_path, log_path) - log_path is where the script's own progress/
    error log gets written (see build_preset_script).
    """
    exe = find_paraview_exe()
    if exe is None:
        raise FileNotFoundError(
            "ParaView doesn't appear to be installed (checked Program Files) - "
            "install it, or open case.foam manually from the case directory."
        )
    fd, script_path = tempfile.mkstemp(suffix=".py", prefix="guvcfd_paraview_")
    log_path = script_path + ".log"
    script = build_preset_script(case_dir, mesh_bounds, log_path, source_center=source_center)
    with open(fd, "w") as f:
        f.write(script)
    subprocess.Popen([exe, f"--script={script_path}"])
    return script_path, log_path
