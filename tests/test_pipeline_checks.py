"""Run each stage's self-checks under pytest.

The surface and attribution stages ship their checks as standalone scripts
(63 assertions in total, the ones the README cites). This wrapper runs each
script in its own directory and fails if any assertion fails, so `pytest`
is the single entry point for CI.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("stage", ["surface", "attribution"])
def test_stage_sanity_checks(stage: str) -> None:
    proc = subprocess.run(
        [sys.executable, "tests/test_sanity.py"],
        cwd=ROOT / stage, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "FAIL" not in proc.stdout.replace("ALL PASS", "")
