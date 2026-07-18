"""Splice constant/fvOptions into the scalarTransport function object's
nested fvOptions{} block inside system/controlDict.

We're not running a normal solver that reads constant/fvOptions on its own -
the UV scalar transport is handled by the `scalarTransport` function object
attached in controlDict, which only sees sources copy-pasted into its own
nested fvOptions{} sub-block. Every time the mesh (and therefore
constant/fvOptions' cellZone contents) is regenerated, that nested block goes
stale and must be re-spliced with the fresh content.
"""
import re


def _read_fvoptions_body(fvoptions_path):
    """Return constant/fvOptions' content with the FoamFile header stripped."""
    with open(fvoptions_path) as f:
        content = f.read()
    # Strip the FoamFile{...} header block, keep everything after its closing '}'.
    m = re.search(r'^FoamFile\s*\n\{.*?\n\}\s*\n', content, re.DOTALL | re.MULTILINE)
    if not m:
        raise RuntimeError(f"Could not find FoamFile header block in {fvoptions_path}")
    return content[m.end():].strip("\n")


def _find_matching_brace(text, open_brace_pos):
    """Given the index of an opening '{', return the index of its matching '}'."""
    depth = 0
    i = open_brace_pos
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise RuntimeError("Unbalanced braces: no matching '}' found")


def splice_fv_options_into_control_dict(case_dir, indent="        "):
    """Replace the stale nested fvOptions{} block inside controlDict's
    scalarTransport function object with the freshly-generated
    constant/fvOptions content. Returns (controlDict_path, n_open, n_close)
    so the caller can verify brace balance.
    """
    fv_body = _read_fvoptions_body(f"{case_dir}/constant/fvOptions")
    indented_body = "\n".join(indent + line if line else "" for line in fv_body.splitlines())

    cd_path = f"{case_dir}/system/controlDict"
    with open(cd_path) as f:
        content = f.read()

    m = re.search(r'\n(\s*)fvOptions\s*\n(\s*)\{', content)
    if not m:
        raise RuntimeError("Could not find 'fvOptions' block inside controlDict")
    keyword_indent = m.group(1)
    open_brace_pos = content.index("{", m.end() - 1)
    close_brace_pos = _find_matching_brace(content, open_brace_pos)

    new_content = (
        content[:open_brace_pos + 1]
        + "\n" + indented_body + "\n" + keyword_indent
        + content[close_brace_pos:]
    )

    with open(cd_path, "w") as f:
        f.write(new_content)

    n_open = new_content.count("{")
    n_close = new_content.count("}")
    return cd_path, n_open, n_close


def splice_into_functions_block(case_dir, block_text):
    """Insert `block_text` (one or more already-indented function-object
    entries, e.g. from monitoring.live_vol_average_functions()) as a new
    sibling inside controlDict's outer functions{} block (where
    scalarTransport1 already lives), just before its closing brace.
    Returns (controlDict_path, n_open, n_close) so the caller can verify
    brace balance, same convention as splice_fv_options_into_control_dict.
    """
    cd_path = f"{case_dir}/system/controlDict"
    with open(cd_path) as f:
        content = f.read()

    m = re.search(r'\n(\s*)functions\s*\n(\s*)\{', content)
    if not m:
        raise RuntimeError("Could not find 'functions' block inside controlDict")
    keyword_indent = m.group(1)
    open_brace_pos = content.index("{", m.end() - 1)
    close_brace_pos = _find_matching_brace(content, open_brace_pos)

    new_content = (
        content[:close_brace_pos]
        + "\n" + block_text + "\n" + keyword_indent
        + content[close_brace_pos:]
    )

    with open(cd_path, "w") as f:
        f.write(new_content)

    n_open = new_content.count("{")
    n_close = new_content.count("}")
    return cd_path, n_open, n_close


def set_function_object_enabled(case_dir, function_name, enabled):
    """Set (or insert) an `enabled` entry at the top of a functions{}
    sub-dict in controlDict - e.g. to disable scalarTransport1 while running
    simpleFoam. Every solver reading this controlDict executes every
    function object listed in it, including scalarTransport1's UV-decay
    fvOptions sink terms - solving that against a wildly unconverged early
    flow field (simpleFoam's first iterations after a mapFields warm start)
    causes a floating-point blowup. Re-enable before running pimpleFoam.
    """
    cd_path = f"{case_dir}/system/controlDict"
    with open(cd_path) as f:
        content = f.read()

    m = re.search(rf'\n(\s*){re.escape(function_name)}\s*\n(\s*)\{{', content)
    if not m:
        raise RuntimeError(f"Could not find '{function_name}' block inside controlDict")
    body_indent = m.group(2) + "    "
    open_brace_pos = content.index("{", m.end() - 1)

    after_open = open_brace_pos + 1
    existing = re.match(r'\n\s*enabled\s+\w+\s*;', content[after_open:after_open + 60])
    if existing:
        content = content[:after_open] + content[after_open + existing.end():]

    value = "true" if enabled else "false"
    new_content = (
        content[:after_open]
        + f"\n{body_indent}enabled         {value};"
        + content[after_open:]
    )
    with open(cd_path, "w") as f:
        f.write(new_content)
    return cd_path


def set_control_dict_time(case_dir, end_time=None, write_interval=None, delta_t=None):
    """Set endTime/writeInterval/deltaT in controlDict. Used to give
    simpleFoam its own iteration budget separate from pimpleFoam's transient
    duration, since they share this one controlDict but mean completely
    different things (iterations vs. physical seconds).

    writeInterval is replaced everywhere it appears, not just the top-level
    occurrence - scalarTransport1 has its *own* nested writeInterval
    (independent of the main solver's), and if left unsynced, T only gets
    written on that separate schedule while U/p/k/omega/nut follow the main
    one, leaving T missing from most time directories. endTime/deltaT aren't
    duplicated per-function-object, so those stay first-occurrence-only.
    """
    cd_path = f"{case_dir}/system/controlDict"
    with open(cd_path) as f:
        content = f.read()
    if end_time is not None:
        content = re.sub(r'(\n[ \t]*)endTime(\s+)[\d.]+;', rf'\g<1>endTime\g<2>{end_time};', content, count=1)
    if write_interval is not None:
        content = re.sub(r'(\n[ \t]*)writeInterval(\s+)[\d.]+;', rf'\g<1>writeInterval\g<2>{write_interval};', content)
    if delta_t is not None:
        content = re.sub(r'(\n[ \t]*)deltaT(\s+)[\d.]+;', rf'\g<1>deltaT\g<2>{delta_t};', content, count=1)
    with open(cd_path, "w") as f:
        f.write(content)
    return cd_path


def set_function_write_interval(case_dir, function_name, value):
    """Set a specific functions{} sub-block's own writeInterval, scoped to
    just that block - unlike set_control_dict_time's blanket sweep across
    the whole file. Used to re-pin a live-tracking function object's
    writeInterval at 1 (every iteration) after a set_control_dict_time()
    call for a later phase would otherwise clobber it back to that phase's
    (much sparser) write_interval - see steady_state_pipeline._run_phase.
    """
    cd_path = f"{case_dir}/system/controlDict"
    with open(cd_path) as f:
        content = f.read()

    m = re.search(rf'\n(\s*){re.escape(function_name)}\s*\n(\s*)\{{', content)
    if not m:
        raise RuntimeError(f"Could not find '{function_name}' block inside controlDict")
    open_brace_pos = content.index("{", m.end() - 1)
    close_brace_pos = _find_matching_brace(content, open_brace_pos)

    body = content[open_brace_pos:close_brace_pos]
    new_body, n = re.subn(r'(\n[ \t]*writeInterval\s+)[\d.]+;', rf'\g<1>{value};', body)
    if n == 0:
        raise RuntimeError(f"No writeInterval found inside '{function_name}' block")

    new_content = content[:open_brace_pos] + new_body + content[close_brace_pos:]
    with open(cd_path, "w") as f:
        f.write(new_content)
    return cd_path


def set_control_dict_start_from(case_dir, mode):
    """Set controlDict's startFrom to "latestTime" (resume the solver from
    whatever time directory is already on disk) or "startTime" (the normal
    fresh-run default) - used to extend an already-run decay simulation to a
    longer duration without redoing mesh generation or flow convergence.

    Both the solver *and* `postProcess -dict ...` honor this setting for
    their own time range (verified directly - left on latestTime,
    `postProcess` only recomputes the single newest time step instead of the
    full history). So a resume needs latestTime for the solver step, then
    startTime again (with endTime left at its new, higher value) before
    postProcess runs, to get one continuous merged decay curve back.
    """
    cd_path = f"{case_dir}/system/controlDict"
    with open(cd_path) as f:
        content = f.read()
    content = re.sub(r'(\n[ \t]*)startFrom(\s+)\w+;', rf'\g<1>startFrom\g<2>{mode};', content, count=1)
    with open(cd_path, "w") as f:
        f.write(content)
    return cd_path


_SIMPLE_BLOCK = """
SIMPLE
{
    nNonOrthogonalCorrectors 0;
    consistent      no;
    residualControl
    {
        p               1e-4;
        U               1e-4;
        "(k|omega)"     1e-4;
    }
}

relaxationFactors
{
    fields
    {
        p               0.3;
    }
    equations
    {
        U               0.7;
        "(k|omega)"     0.7;
        T               0.7;
    }
}
"""


def ensure_simple_fvsolution(case_dir):
    """Append a SIMPLE{} + relaxationFactors{} block to fvSolution if not
    already present, so simpleFoam has under-relaxation to run stably.

    fvSolution here (like the working reference case it's copied from) was
    only ever set up for PIMPLE (transient) - no SIMPLE block, no
    relaxationFactors at all. Without under-relaxation, the SIMPLE algorithm
    is well-known to be unstable, which is exactly what caused the
    unrelaxed-momentum-solve blowup. Solver sections are name-scoped
    (simpleFoam only reads SIMPLE{}, pimpleFoam only reads PIMPLE{}), so
    both can coexist in the same file with no conflict - this is additive,
    not a toggle like set_function_object_enabled.
    """
    fvs_path = f"{case_dir}/system/fvSolution"
    with open(fvs_path) as f:
        content = f.read()
    if re.search(r'\nSIMPLE\s*\n\s*\{', content):
        return fvs_path  # already present, nothing to do
    with open(fvs_path, "w") as f:
        f.write(content.rstrip("\n") + "\n" + _SIMPLE_BLOCK)
    return fvs_path


def set_relaxation_factors(case_dir, momentum_factor=None, scalar_factor=None):
    """Overwrite fvSolution's relaxationFactors{}.equations entries for
    U/(k|omega) (momentum_factor) and T (scalar_factor) - GUI-exposed as
    cross-project "advanced" defaults (Settings menu), like flow_rel_tol/
    cell_size above.

    Under-relaxation damps SIMPLE's outer iteration: each pass, instead of
    fully accepting the newly-solved value for a field, only a fraction of
    the change is taken (factor 1.0 = no damping, most prone to overshoot;
    lower = slower but more resistant to oscillating/diverging). Momentum
    (U) and turbulence (k, omega) share one factor since they're already
    grouped under a single "(k|omega)" regex entry in the template; T gets
    its own since a stiff/strong source or sink term interacting with the
    flow field can destabilize the scalar transport independently of
    whether momentum itself is well-behaved (see steady_state_pipeline's
    docstring / CHANGELOG for the real case this fixed).

    None (either arg) leaves that entry at whatever the template already
    has - only touches what's explicitly asked for.
    """
    fvs_path = f"{case_dir}/system/fvSolution"
    with open(fvs_path) as f:
        content = f.read()
    if momentum_factor is not None:
        content = re.sub(r'(\n[ \t]*U\s+)[\d.]+;', rf'\g<1>{momentum_factor};', content, count=1)
        content = re.sub(r'(\n[ \t]*"\(k\|omega\)"\s+)[\d.]+;', rf'\g<1>{momentum_factor};', content, count=1)
    if scalar_factor is not None:
        content = re.sub(r'(\n[ \t]*T\s+)[\d.]+;', rf'\g<1>{scalar_factor};', content, count=1)
    with open(fvs_path, "w") as f:
        f.write(content)
    return fvs_path


_LTS_DDT_DEFAULT = (
    "    default         localEuler;\n"
    "    rDeltaTSmoothingCoeff 0.1;\n"
    "    rDeltaTDampingCoeff 1;\n"
    "    maxDeltaT       1;"
)


def set_lts_ddt_scheme(case_dir, enabled):
    """Toggle ddtSchemes.default between localEuler (Local Time Stepping -
    each cell gets its own pseudo-timestep sized to its local Courant
    number, converging pseudo-transient flow problems faster than a single
    uniform step for flows with very different length/time scales in
    different regions) and Euler (real time-accurate transient, needed by
    the later pimpleFoam decay run - LTS must NOT still be active then).

    rDeltaTSmoothingCoeff/rDeltaTDampingCoeff/maxDeltaT are the standard
    OpenFOAM LTS controls (limit how fast the local timestep field can grow/
    shrink between neighbouring cells and cap its absolute size) - same
    values used in OpenFOAM's own LTS tutorials (e.g. simpleFoam cases
    converted to pimpleFoam+LTS) as a reasonable starting point.
    """
    path = f"{case_dir}/system/fvSchemes"
    with open(path) as f:
        content = f.read()
    m = re.search(r'ddtSchemes\s*\{.*?\n\}', content, re.DOTALL)
    if not m:
        raise RuntimeError(f"Could not find ddtSchemes{{}} block in {path}")
    replacement = ("ddtSchemes\n{\n" + _LTS_DDT_DEFAULT + "\n}") if enabled else \
        "ddtSchemes\n{\n    default         Euler;\n}"
    content = content[:m.start()] + replacement + content[m.end():]
    with open(path, "w") as f:
        f.write(content)
    return path
