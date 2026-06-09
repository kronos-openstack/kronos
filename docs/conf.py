# Sphinx configuration for the Kronos documentation site.

project = "Kronos"
copyright = "2026, Kronos contributors"
author = "Kronos contributors"

extensions: list[str] = []

templates_path: list[str] = []
exclude_patterns = ["_build"]

html_theme = "furo"
html_title = "Kronos"

# The generated config reference is plain INI, included verbatim.
html_static_path: list[str] = []
