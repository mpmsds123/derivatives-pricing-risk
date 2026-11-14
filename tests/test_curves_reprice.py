"""The SOFR bootstrap must reprice every one of its pillar inputs.

`curves/` reports its self-consistency check only in `bootstrap_diagnostics.txt`;
nothing failed a run if it broke. This rebuilds the curve from the committed
inputs and asserts that all 21 pillars (7 SR3 futures, 14 OIS swaps) come back
within the module's own 0.01 bp tolerance.

Run in a subprocess because `curves/` and `attribution/` both use the package
path `src` under different roots and cannot share one interpreter.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_PROBE = r"""
import sys, json
import pandas as pd
sys.path.insert(0, sys.argv[1])
from run import build_curve_grid, par_rate_check
from src.bootstrap import bootstrap
from src.calendar_utils import BusinessCalendar
from src.config import CONFIG as cfg
from src.curve import Curve
from src.instruments import build_instruments, load_fixings, load_futures, load_swaps

cal = BusinessCalendar(cfg["calendar_start"], cfg["curve_horizon"])
template = Curve(pd.Timestamp(cfg["valuation_date"]), build_curve_grid(cal, cfg))
fixings = load_fixings(cfg["raw_data_dir"] / cfg["fixings_file"])
sr1 = load_futures(cfg["raw_data_dir"] / cfg["sr1_file"])
sr3 = load_futures(cfg["raw_data_dir"] / cfg["sr3_file"])
swaps = load_swaps(cfg["data_dir"] / cfg["swap_file"])
instruments, cross_check = build_instruments(cal, template, fixings, sr1, sr3, swaps, cfg)
curve, _ = bootstrap(template, instruments, cfg)
checks = par_rate_check(curve, instruments, [], cfg)
print(json.dumps({
    "kinds": checks["kind"].value_counts().to_dict(),
    "max_abs_error_bp": float(checks["error_bp"].abs().max()),
    "tolerance_bp": cfg["self_consistency_tolerance_bp"],
}))
"""


def test_curve_reprices_all_21_pillars() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(ROOT / "curves")],
        cwd=ROOT / "curves", capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    res = json.loads(proc.stdout.strip().splitlines()[-1])
    assert res["kinds"] == {"SWAP": 14, "SR3": 7}, res["kinds"]
    assert res["tolerance_bp"] == 0.01
    assert res["max_abs_error_bp"] < 0.01, res
