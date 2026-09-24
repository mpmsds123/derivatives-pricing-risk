"""US business day calendar, roll conventions, IMM dates, accrual schedules."""

from __future__ import annotations

import numpy as np
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar

_WEDNESDAY = 2

_MONTH_CODE: dict[str, int] = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}


class BusinessCalendar:
    """US federal holiday calendar, precomputed as a flat business day index.

    Building the index once and using `searchsorted` everywhere keeps the
    rebootstrap loop (28 curves for the key-rate ladder) cheap.
    """

    def __init__(self, start: str | pd.Timestamp, end: str | pd.Timestamp) -> None:
        start = pd.Timestamp(start)
        end = pd.Timestamp(end)
        all_days = pd.date_range(start, end, freq="D")
        weekdays = all_days[all_days.dayofweek < 5]
        holidays = USFederalHolidayCalendar().holidays(start, end)
        self.days: pd.DatetimeIndex = weekdays.difference(holidays)

    def __len__(self) -> int:
        return len(self.days)

    def is_business_day(self, d: pd.Timestamp) -> bool:
        d = pd.Timestamp(d)
        i = self.days.searchsorted(d, "left")
        return bool(i < len(self.days) and self.days[i] == d)

    def following(self, d: pd.Timestamp) -> pd.Timestamp:
        """First business day on or after `d`."""
        return self.days[self.days.searchsorted(pd.Timestamp(d), "left")]

    def preceding(self, d: pd.Timestamp) -> pd.Timestamp:
        """Last business day on or before `d`."""
        return self.days[self.days.searchsorted(pd.Timestamp(d), "right") - 1]

    def modified_following(self, d: pd.Timestamp) -> pd.Timestamp:
        """Following, unless that crosses into the next month, then preceding."""
        d = pd.Timestamp(d)
        nxt = self.following(d)
        return self.preceding(d) if nxt.month != d.month else nxt

    def add_business_days(self, d: pd.Timestamp, n: int) -> pd.Timestamp:
        i = int(self.days.searchsorted(pd.Timestamp(d), "left"))
        return self.days[i + n]


def imm_date(year: int, month: int) -> pd.Timestamp:
    """Third Wednesday of the month -- the CME SR3 / IMM roll date."""
    first = pd.Timestamp(year=year, month=month, day=1)
    offset = (_WEDNESDAY - first.dayofweek) % 7
    return first + pd.Timedelta(days=offset + 14)


def parse_contract_code(code: str) -> tuple[str, int, int]:
    """`SR3H26` -> ('SR3', 2026, 3)."""
    code = code.strip().upper()
    product, month_letter, yy = code[:3], code[3], code[4:]
    if month_letter not in _MONTH_CODE:
        raise ValueError(f"unrecognised CME month code {month_letter!r} in {code!r}")
    return product, 2000 + int(yy), _MONTH_CODE[month_letter]


def accrual_schedule(
    cal: BusinessCalendar, start: pd.Timestamp, end: pd.Timestamp
) -> tuple[pd.DatetimeIndex, np.ndarray]:
    """Overnight accrual breakdown of the half-open interval [start, end).

    Returns the business days whose SOFR fixing applies, and the number of
    calendar days each fixing covers. If `start` is not a business day the
    preceding business day's fixing carries the weekend/holiday, which is the
    published SOFR convention.

    Invariant: `counts.sum() == (end - start).days`.
    """
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    if end <= start:
        raise ValueError(f"empty accrual window [{start.date()}, {end.date()})")

    bd = cal.days
    i = int(bd.searchsorted(start, "left"))
    j = int(bd.searchsorted(end, "left"))
    if i >= len(bd):
        raise ValueError(f"accrual start {start.date()} is beyond the calendar")
    if bd[i] > start:
        i = max(i - 1, 0)

    setting_days = bd[i:j]
    if len(setting_days) == 0:
        raise ValueError(f"no fixings in [{start.date()}, {end.date()})")

    starts = setting_days.values.copy()
    starts[0] = max(starts[0], np.datetime64(start))
    ends = np.append(starts[1:], np.datetime64(end))
    counts = (ends - starts).astype("timedelta64[D]").astype(np.int64)

    expected = (end - start).days
    if counts.sum() != expected:
        raise AssertionError(f"accrual day counts {counts.sum()} != calendar span {expected}")

    return setting_days, counts
