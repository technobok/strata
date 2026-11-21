"""Locate repo-root directories (database/migrations, docs) in every layout.

The package runs from a source checkout (dev, tests) or from site-packages in
the container image, where the Dockerfile copies these directories to /app.
"""

import os
from pathlib import Path


def repo_dir(relative: str) -> Path:
    """The ``relative`` directory of the strata checkout or image, e.g. ``docs``."""
    candidates = [
        # Source checkout: src/strata/paths.py -> repo_root/<relative>
        Path(__file__).parent.parent.parent / relative,
        # Container / installed layout: $STRATA_ROOT/<relative>
        *([Path(os.environ["STRATA_ROOT"]) / relative] if os.environ.get("STRATA_ROOT") else []),
        # CWD fallback (e.g. running from /app in docker)
        Path.cwd() / relative,
    ]
    for path in candidates:
        if path.is_dir():
            return path
    raise FileNotFoundError(f"Could not locate {relative}/. Tried: {[str(p) for p in candidates]}")
