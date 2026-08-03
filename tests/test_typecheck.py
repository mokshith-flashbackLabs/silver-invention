"""mypy strict is a blocking check — NewType is worthless without it
(CLAUDE.md §10, build spec Phase 1 §2)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_mypy(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, MYPYPATH=str(ROOT / "src"))
    return subprocess.run(
        [sys.executable, "-m", "mypy", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=300,
    )


def test_package_passes_mypy_strict() -> None:
    result = _run_mypy()  # configuration (strict, packages) comes from pyproject.toml
    assert result.returncode == 0, f"mypy strict failed:\n{result.stdout}{result.stderr}"


def test_bare_str_where_user_ref_expected_fails() -> None:
    fixture = ROOT / "tests" / "fixtures_typecheck" / "bad_ids.py"
    result = _run_mypy("--strict", str(fixture))
    assert result.returncode != 0, "mypy accepted a bare str as UserRef — NewType gate is dead"
    # All four deliberate violations must be reported.
    assert result.stdout.count("error:") >= 4, result.stdout
