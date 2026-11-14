"""The Hagan SABR formula is vendored twice; this pins the two copies together.

`attribution/src/hagan.py` is a deliberate copy of `surface/src/hagan.py` so the
attribution project runs standalone (stated in its module docstring). Nothing
otherwise stops the two drifting, and a drift would move every attribution and
risk number while every existing check still passed.

Each copy is imported in its own subprocess, because both live at the package
path `src.hagan` under different roots and cannot share one interpreter.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# One deep-wing strike ladder around a SPY-like forward, plus two parameter sets:
# the calibrated short-dated regime (high nu) and a long-dated one (low nu).
_PROBE = r"""
import sys, json
import numpy as np
sys.path.insert(0, sys.argv[1])
from src.hagan import sabr_vol

F, T, BETA = 775.0, 1.0, 0.5
out = []
for alpha, rho, nu in ((4.5, -0.6, 0.8), (3.59, -0.5034, 2.1523), (5.70, -0.6173, 0.3195)):
    vols = np.atleast_1d(sabr_vol(np.linspace(200.0, 1400.0, 41), F, T, alpha, BETA, rho, nu))
    out.extend(float(v) for v in vols)
print(json.dumps(out))
"""


def _vols(module_root: Path) -> list[float]:
    proc = subprocess.run([sys.executable, "-c", _PROBE, str(module_root)],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


def test_vendored_hagan_copies_agree() -> None:
    surface = _vols(ROOT / "surface")
    attribution = _vols(ROOT / "attribution")
    assert len(surface) == len(attribution) == 123
    worst = max(abs(a - b) for a, b in zip(surface, attribution))
    assert worst == pytest.approx(0.0, abs=1e-15), (
        f"surface/src/hagan.py and attribution/src/hagan.py disagree by {worst:.3e} "
        "in implied vol; they are supposed to be identical formulas"
    )
