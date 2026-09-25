"""Every script and module must at least compile. The suite imports scanner/
but never runs scripts/run_live.py, so a syntax error there shipped in v1.2.1
and v1.3.0 and only showed when the scanner was started."""
import py_compile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted(p for d in ("scripts", "scanner") for p in (ROOT / d).rglob("*.py") if "__pycache__" not in p.parts)


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_compiles(path):
    py_compile.compile(str(path), doraise=True, cfile=None)
