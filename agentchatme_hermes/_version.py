"""Single source of truth for the package version.

``pyproject.toml`` reads this via ``[tool.hatch.version]`` so a release
bump touches one file, not two.
"""
from __future__ import annotations

__version__ = "0.2.0"
