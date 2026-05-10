"""Single source of truth for the package version.

Kept in a tiny module of its own so ``pyproject.toml``'s release machinery
and the runtime ``__version__`` cannot drift. Bump in patch increments per
the project's release policy (0.1.0 → 0.1.1 → 0.1.2 …) until a full 1.0
cut is justified by real-fleet traffic.
"""

from __future__ import annotations

__version__ = "0.1.6"
