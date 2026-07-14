from guvcfd.paraview_launch import build_preset_script, _screenshot_lines

_MESH_BOUNDS = (0.0, 3.2, 0.0, 4.8, 0.0, 2.57)


def _build(source_center=None):
    return build_preset_script(
        case_dir="/tmp/case", mesh_bounds=_MESH_BOUNDS, log_path="/tmp/case/pv.log",
        source_center=source_center,
    )


def test_script_renders_without_a_source_center():
    script = _build(source_center=None)
    assert "sourceStreamTracer" not in script
    assert "view3" not in script
    # log-scale T setup is unconditional - applies to decay scenarios too.
    assert "UseLogScale = 1" in script
    assert "MapControlPointsToLogSpace" in script


def test_script_adds_third_view_when_source_center_given():
    script = _build(source_center=(0.5, 1.8, 1.3))
    assert "sourceStreamTracer" in script
    assert "sourceStreamTracer.SeedType.Center = [0.5, 1.8, 1.3]" in script
    assert "ColorBy(disp3, ('POINTS', 'T'))" in script
    # Deliberately not rescaled again - see the comment in _VIEW3_TEMPLATE.
    assert "disp3.RescaleTransferFunctionToDataRangeOverTime" not in script
    # view3 no longer depends on nested layout cell-index guessing - see the
    # comment in _VIEW3_TEMPLATE about why that was replaced (the comment
    # itself still mentions the old approach by name, hence checking for
    # the removed call, not just the string "AssignView(4").
    assert "CreateLayout" in script
    assert "layout1.AssignView(4, view3)" not in script


def test_script_is_valid_python_syntax():
    # The template uses manual {{ }} escaping for embedded f-strings inside
    # a .format() call - easy to get wrong (stray single/double braces) in
    # a way that only surfaces once pasted into a real (detached, hard to
    # debug) ParaView process. Catch that here instead.
    import ast
    for source_center in (None, (0.5, 1.8, 1.3)):
        script = _build(source_center=source_center)
        ast.parse(script)  # raises SyntaxError if malformed


def test_stream_tracer_seeded_at_room_center_not_inlet():
    # A tight seed cloud right at the inlet just retraces the jet core
    # already visible in the T volume render and says nothing about the
    # room's broader circulation - verified directly against a real case,
    # it produced a single redundant bundle of near-identical lines. The
    # seed must cover the room broadly instead.
    script = _build()
    xmin, xmax, ymin, ymax, zmin, zmax = _MESH_BOUNDS
    expected_center = [(xmin + xmax) / 2, (ymin + ymax) / 2, (zmin + zmax) / 2]
    assert f"streamTracer.SeedType.Center = {expected_center}" in script
    # Radius must circumscribe the full bounding box, not just a small patch.
    dx, dy, dz = xmax - xmin, ymax - ymin, zmax - zmin
    expected_radius = ((dx ** 2 + dy ** 2 + dz ** 2) ** 0.5) / 2
    assert f"streamTracer.SeedType.Radius = {expected_radius}" in script


def test_screenshot_lines_handles_two_and_three_paths():
    assert _screenshot_lines(None, None) == ""
    assert _screenshot_lines("a.png", "b.png") == (
        'SaveScreenshot(r"a.png", view1, ImageResolution=[900, 700])\n'
        '    SaveScreenshot(r"b.png", view2, ImageResolution=[900, 700])'
    )
    lines = _screenshot_lines("a.png", "b.png", "c.png")
    assert 'SaveScreenshot(r"c.png", view3, ImageResolution=[900, 700])' in lines
