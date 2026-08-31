"""
Experimental breathing inlet setup: replace volumetric contaminant source
with a small velocity-driven inlet at the injection-zone location.

Converts a source cellZone + volumetric Su term into a physical inlet patch
(0.2m × 0.1m, velocity 0.06 m/s) carrying T at concentration calculated to
deliver the same total T rate as the original G.
"""
from pathlib import Path
import re
import numpy as np
from .wsl_utils import read_case_file, write_case_file, run_wsl_or_raise
from .contaminant_source import _source_box


def add_breathing_inlet_to_mesh(case_dir, source_center, source_size, inlet_T_concentration,
                                 cell_size=None, room_dims=None, log_fn=None):
    """
    Post-mesh modification: add a small breathing inlet patch at the injection
    zone location. Creates a new patch face representing the inlet opening
    (0.2m × 0.1m, grid-aligned), positioned at the source zone center.

    Since the mesh is already committed, this adds geometry via blockMeshDict
    modification or castellation - for now, a simpler approach: add the patch
    directly to the boundary file post-mesh by creating synthetic faces.

    Args:
        case_dir: OpenFOAM case directory
        source_center: (x, y, z) injection zone center
        source_size: injection zone cube half-width
        inlet_T_concentration: T value [T-units/m³] to set on this inlet
        cell_size: mesh cell size (for grid alignment)
        room_dims: (Lx, Ly, Lz) room dimensions
        log_fn: logging callback

    NOTE: This is complex (requires adding faces to an existing mesh). For a
    first experimental pass, consider a simpler fallback: keep the volumetric
    source, but add velocity to the source zone via a momentum source term
    instead. Or: modify the mesh generation BEFORE setup_case() runs.
    """
    log_fn = log_fn or (lambda m: None)

    # For now, log the intent and note the limitation
    log_fn(f"Breathing inlet: would add {inlet_T_concentration:.1f} T inlet at {source_center}")
    log_fn("(Full mesh modification implementation pending - see breathing_inlet.py docstring)")

    # Placeholder: the real implementation would:
    # 1. Read 0/U, 0/T boundary fields
    # 2. Add new inlet patch entries
    # 3. Modify constant/polyMesh/boundary to include the new patch
    # 4. Set inlet velocity and T BCs
    # 5. Disable fvOptions' contaminant_source entry


def disable_volumetric_source(case_dir, log_fn=None):
    """
    Post-case modification: comment out or remove the volumetric contaminant
    source term from fvOptions, since injection is now via inlet patch.

    Reads constant/fvOptions, finds the scalarSemiImplicitSource block for
    'contaminant_source', and disables it (or removes it).
    """
    log_fn = log_fn or (lambda m: None)

    fvoptions_path = f"{case_dir}/constant/fvOptions"
    content = read_case_file(case_dir, "constant/fvOptions")

    # Find and disable contaminant_source entry
    if "contaminant_source" in content:
        # Replace the entry name with a comment, or set enabled false
        modified = re.sub(
            r"(\s+)contaminant_source\s*\{",
            r"// contaminant_source_DISABLED\n\1// \1contaminant_source {",
            content
        )

        if modified != content:
            log_fn("Disabled volumetric contaminant_source in fvOptions")
            write_case_file(case_dir, "constant/fvOptions", modified)
        else:
            log_fn("WARNING: could not disable contaminant_source (pattern not found)")


def set_inlet_boundary_conditions(case_dir, patch_name, inlet_velocity, inlet_T,
                                   log_fn=None):
    """
    Set inlet BCs for a named patch:
    - U: fixedValue [inlet_velocity 0 0]
    - T: fixedValue [inlet_T]

    (Assumes the patch already exists in the mesh boundary.)
    """
    log_fn = log_fn or (lambda m: None)

    # Modify 0/U
    u_path = f"{case_dir}/0/U"
    u_content = read_case_file(case_dir, "0/U")

    # Find or create the patch entry in boundaryField
    if patch_name not in u_content:
        log_fn(f"WARNING: {patch_name} not found in 0/U boundary")
        return

    # Replace with fixedValue
    u_bc = f"""
    {patch_name}
    {{
        type            fixedValue;
        value           uniform ({inlet_velocity} 0 0);
    }}
"""
    # Simple replacement - assumes clean format
    u_modified = re.sub(
        rf"(\s+){patch_name}\s*\{{[^}}]*\}}",
        u_bc,
        u_content,
        flags=re.DOTALL
    )

    if u_modified != u_content:
        log_fn(f"Set U BC for {patch_name}: fixedValue {inlet_velocity} m/s")
        write_case_file(case_dir, "0/U", u_modified)

    # Modify 0/T (if it exists)
    t_path = f"{case_dir}/0/T"
    try:
        t_content = read_case_file(case_dir, "0/T")

        t_bc = f"""
    {patch_name}
    {{
        type            fixedValue;
        value           uniform {inlet_T};
    }}
"""
        t_modified = re.sub(
            rf"(\s+){patch_name}\s*\{{[^}}]*\}}",
            t_bc,
            t_content,
            flags=re.DOTALL
        )

        if t_modified != t_content:
            log_fn(f"Set T BC for {patch_name}: fixedValue {inlet_T} T-units/m³")
            write_case_file(case_dir, "0/T", t_modified)
    except Exception as e:
        log_fn(f"Note: could not modify 0/T ({e})")
