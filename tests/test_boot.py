"""Boot-contract tests: the process must exit non-zero with a clear message.

These run ``python -m imageshield`` in a subprocess from a temp cwd so no
``.env.local`` can leak in; only invalid configurations are exercised (a
valid one would start a server).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import VALID_ENV

_PASSTHROUGH = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "PATHEXT", "COMSPEC")


def _boot(env_overrides: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {key: os.environ[key] for key in _PASSTHROUGH if key in os.environ}
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "imageshield"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
    )


def test_missing_key_exits_nonzero_with_clear_message(tmp_path: Path) -> None:
    env = dict(VALID_ENV)
    del env["DATABASE_URL"]
    result = _boot(env, tmp_path)
    assert result.returncode == 1
    assert "DATABASE_URL" in result.stderr
    assert "Invalid configuration" in result.stderr


def test_equal_tokens_refuse_to_boot(tmp_path: Path) -> None:
    env = dict(VALID_ENV, ADMIN_SERVICE_TOKEN=VALID_ENV["SERVICE_TOKEN"])
    result = _boot(env, tmp_path)
    assert result.returncode == 1
    assert "must differ" in result.stderr


def test_malformed_key_exits_nonzero(tmp_path: Path) -> None:
    env = dict(VALID_ENV, LIVENESS_MIN_CONFIDENCE="very-confident")
    result = _boot(env, tmp_path)
    assert result.returncode == 1
    assert "LIVENESS_MIN_CONFIDENCE" in result.stderr
