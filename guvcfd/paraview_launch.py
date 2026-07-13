"""Launch ParaView pointed at a case directory with a useful default view
already set up, instead of a blank scene the user has to configure by hand
every time: a volume render of T (contamination) in one view, and a Stream
Tracer following flow from the inlet toward the outlet, colored by U, in a
second view.

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

    view1 = GetActiveViewOrCreate('RenderView')
    view1.ViewSize = [900, 700]
    disp1 = Show(reader, view1)
    disp1.SetRepresentationType('Volume')
    ColorBy(disp1, ('POINTS', 'T'))
    disp1.RescaleTransferFunctionToDataRange(True)
    ResetCamera(view1)
    _log_step("view1 (volume T) shown")

    streamTracer = StreamTracer(Input=reader, SeedType='Point Cloud')
    streamTracer.Vectors = ['POINTS', 'U']
    streamTracer.SeedType.Center = [{inlet_x}, {inlet_y}, {inlet_z}]
    streamTracer.SeedType.Radius = {seed_radius}
    streamTracer.SeedType.NumberOfPoints = 80
    streamTracer.MaximumStreamlineLength = {max_length}
    streamTracer.UpdatePipeline()
    _log_step("stream tracer computed")

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
        view2 = CreateRenderView()

    view2.ViewSize = [900, 700]
    disp2 = Show(streamTracer, view2)
    ColorBy(disp2, ('POINTS', 'U'))
    disp2.RescaleTransferFunctionToDataRange(True)
    Show(reader, view2).Opacity = 0.08
    ResetCamera(view2)
    _log_step("view2 (stream tracer U) shown")

    RenderAllViews()
    _log_step("rendered")
    {screenshot_lines}
    _log_step("DONE-OK")
except Exception:
    _log_step("FAILED:\\n" + traceback.format_exc())
finally:
    _log.close()
'''


def _screenshot_lines(view1_png, view2_png):
    if not view1_png and not view2_png:
        return ""
    lines = []
    if view1_png:
        lines.append(f'SaveScreenshot(r"{view1_png}", view1, ImageResolution=[900, 700])')
    if view2_png:
        lines.append(f'SaveScreenshot(r"{view2_png}", view2, ImageResolution=[900, 700])')
    return "\n    ".join(lines)


def build_preset_script(case_dir, inlet_wall, inlet_y, inlet_z, inlet_size,
                         mesh_bounds, log_path, screenshot_paths=(None, None)):
    """mesh_bounds: (xmin, xmax, ymin, ymax, zmin, zmax) - used to place the
    stream tracer seed just inside the inlet opening (a small inward offset
    from the wall, not exactly on it). log_path: where the script writes its
    own step-by-step progress/traceback - launched as a detached GUI process,
    so stdout/stderr redirection from the launcher side isn't reliable.
    """
    xmin, xmax = mesh_bounds[0], mesh_bounds[1]
    offset = 0.05
    inlet_x = xmin + offset if inlet_wall == "xMin" else xmax - offset
    seed_radius = max(min(inlet_size) / 2, 0.05)
    max_length = (xmax - xmin) * 3
    return _SCRIPT_TEMPLATE.format(
        case_foam=f"{case_dir}/case.foam",
        inlet_x=inlet_x, inlet_y=inlet_y, inlet_z=inlet_z,
        seed_radius=seed_radius, max_length=max_length,
        log_path=log_path,
        screenshot_lines=_screenshot_lines(*screenshot_paths),
    )


def launch_paraview(case_dir, inlet_wall, inlet_y, inlet_z, inlet_size, mesh_bounds):
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
    script = build_preset_script(case_dir, inlet_wall, inlet_y, inlet_z, inlet_size,
                                  mesh_bounds, log_path)
    with open(fd, "w") as f:
        f.write(script)
    subprocess.Popen([exe, f"--script={script_path}"])
    return script_path, log_path
