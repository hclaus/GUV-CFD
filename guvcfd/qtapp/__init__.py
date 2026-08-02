"""Native (PySide6/Qt) desktop GUI for GUV-CFD - a parallel, standalone
front-end to the same simulation engine guvcfd/app.py (the Dash web app)
drives. Every pipeline module (run_pipeline, steady_state_pipeline,
scenario_runs, decay_analysis, mesh_gen, ...) is reused completely
unchanged - only the GUI shell differs. guvcfd/app.py is deliberately not
imported/modified here, so the existing web app stays a working fallback
while this one matures.

Launch with `python -m guvcfd.qtapp`.
"""
