"""Relative Volume (RVOL).

For each 5-minute slot of the regular session, compute the 20-day average
volume for that slot.  RVOL = today's cumulative volume divided by the
sum of those slot averages up to the current time of day.

This makes RVOL time-of-day aware: heavy volume in the first 30 minutes
is not compared against the full-day average.
"""
import pandas as pd

_MARKET_OPEN_MINUTES = 9 * 60 + 30   # 9:30 ET = 570 minutes since midnight
_SESSION_MINUTES = 6 * 60 + 30       # 6.5 hour session = 390 minutes


def build_volume_profile(bars_5m: pd.DataFrame) -> pd.Series:
    """Compute per-slot average volume from 20 days of 5-min bars.

    Args:
        bars_5m: UTC-indexed 5-min bar DataFrame with a `volume` column.
                 Should contain ~20 trading days of data.

    Returns:
        Series indexed by minutes-from-open (0, 5, 10, ..., 385),
        values = mean volume across all sessions at that slot.
    """
    et = bars_5m.copy()
    et.index = bars_5m.index.tz_convert("America/New_York")

    minutes_since_midnight = et.index.hour * 60 + et.index.minute
    slot = (minutes_since_midnight // 5) * 5 - _MARKET_OPEN_MINUTES

    et = et.copy()
    et["slot"] = slot.values
    session_bars = et[(et["slot"] >= 0) & (et["slot"] < _SESSION_MINUTES)]

    return session_bars.groupby("slot")["volume"].mean()


def compute_rvol(
    profile: pd.Series,
    cum_vol: float,
    minutes_from_open: int,
) -> float:
    """Current RVOL given the volume profile and today's cumulative volume.

    Args:
        profile:           output of build_volume_profile
        cum_vol:           shares traded today so far
        minutes_from_open: floor to 5-min boundary for current ET time
                           (e.g. 9:37 ET -> 5, since first full slot is 0-4)

    Returns:
        RVOL ratio; NaN if profile is empty or no slots elapsed.
    """
    if profile.empty:
        return float("nan")
    elapsed = profile[profile.index < minutes_from_open]
    if elapsed.empty:
        return float("nan")
    expected = float(elapsed.sum())
    if expected <= 0:
        return float("nan")
    return cum_vol / expected
