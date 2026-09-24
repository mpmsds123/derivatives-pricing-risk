"""Day count fractions.

Convention errors are the single most common source of bp-level pricing
discrepancies, so every accrual in this project routes through `daycount`.
"""

from __future__ import annotations

import pandas as pd

SUPPORTED: tuple[str, ...] = ("ACT/360", "ACT/365F", "30/360")


def daycount(d1: pd.Timestamp, d2: pd.Timestamp, convention: str) -> float:
    """Year fraction between two dates under `convention`.

    ACT/360   -- actual days / 360. SOFR accrual and the USD OIS fixed leg.
    ACT/365F  -- actual days / 365 fixed. Used here only to report zero rates.
    30/360    -- US bond basis (30U/360).
    """
    d1 = pd.Timestamp(d1)
    d2 = pd.Timestamp(d2)

    if convention == "ACT/360":
        return (d2 - d1).days / 360.0
    if convention == "ACT/365F":
        return (d2 - d1).days / 365.0
    if convention == "30/360":
        y1, m1, day1 = d1.year, d1.month, d1.day
        y2, m2, day2 = d2.year, d2.month, d2.day
        if day1 == 31:
            day1 = 30
        if day2 == 31 and day1 == 30:
            day2 = 30
        return ((y2 - y1) * 360 + (m2 - m1) * 30 + (day2 - day1)) / 360.0

    raise ValueError(f"unsupported day count convention {convention!r}; expected one of {SUPPORTED}")
