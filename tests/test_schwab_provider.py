"""Schwab as a full data provider: rate limiting, retries, batch history,
the range-aware cache and the expired-login check. Offline (fake client)."""
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from scanner.data import schwab as sw
from scanner.data.schwab import SchwabFeed, _RateLimiter


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _candles(start: date, n: int):
    return {"candles": [{"datetime": int(pd.Timestamp(start + timedelta(days=i), tz="UTC").timestamp() * 1000),
                         "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1000} for i in range(n)]}


class _Client:
    def __init__(self, fail_first=0):
        self.calls, self.fail_first = [], fail_first

    def price_history(self, symbol, **kw):
        self.calls.append(symbol)
        if self.fail_first:
            self.fail_first -= 1
            return _Resp(429)
        start = kw["startDate"].date()
        return _Resp(200, _candles(start, 10))

    def quotes(self, symbols, fields):
        return _Resp(200, {s: {"quote": {"lastPrice": 20.0}} for s in symbols})


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(sw, "_LIMITER", _RateLimiter(1e9))
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)


def _feed(tmp_path, client):
    return SchwabFeed(cache_dir=tmp_path / "d", intraday_cache_dir=tmp_path / "m", client=client)


def test_rate_limiter_spaces_calls_evenly():
    t = [0.0]
    waits = []
    lim = _RateLimiter(120, clock=lambda: t[0], sleep=waits.append)
    for _ in range(4):
        lim.acquire()
    assert waits == pytest.approx([0.5, 1.0, 1.5])      # 120/min = one every 0.5 s


def test_request_retries_429_then_succeeds(tmp_path):
    c = _Client(fail_first=2)
    df = _feed(tmp_path, c).get_historical_daily("AAA", date(2026, 1, 5), date(2026, 1, 20))
    assert len(c.calls) == 3 and len(df) == 10


def test_daily_multi_fetches_each_symbol_and_reports_progress(tmp_path):
    c, seen = _Client(), []
    got = _feed(tmp_path, c).get_historical_daily_multi(["A", "B", "C"], date(2026, 1, 5), date(2026, 1, 20),
                                                        progress=lambda d, n: seen.append((d, n)))
    assert sorted(got) == ["A", "B", "C"] and sorted(c.calls) == ["A", "B", "C"] and seen[-1] == (3, 3)


def test_cache_is_reused_only_when_it_covers_the_requested_span(tmp_path):
    c = _Client()
    f = _feed(tmp_path, c)
    f.get_historical_daily("AAA", date(2026, 1, 5), date(2026, 1, 20))
    f.get_historical_daily("AAA", date(2026, 1, 7), date(2026, 1, 20))     # inside: cached
    assert c.calls == ["AAA"]
    f.get_historical_daily("AAA", date(2025, 1, 5), date(2026, 1, 20))     # longer: refetch
    assert c.calls == ["AAA", "AAA"]


def test_snapshot_goes_through_the_limiter(tmp_path):
    assert _feed(tmp_path, _Client()).get_snapshot(["A", "B"])["B"]["price"] == 20.0


def _tokens_db(path, issued: datetime):
    with sqlite3.connect(path) as con:
        con.execute("create table schwabdev (access_token_issued text, refresh_token_issued text)")
        con.execute("insert into schwabdev values (?, ?)", (issued.isoformat(), issued.isoformat()))
    return path


def test_expired_login_fails_fast_with_instructions(tmp_path, monkeypatch):
    db = _tokens_db(tmp_path / "t.db", datetime.now(timezone.utc) - timedelta(days=9))
    real = sw.refresh_token_age_days
    monkeypatch.setattr(sw, "refresh_token_age_days", lambda: real(db))
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    with pytest.raises(RuntimeError, match="schwab_auth.py"):
        SchwabFeed(cache_dir=tmp_path / "d", intraday_cache_dir=tmp_path / "m")


def test_refresh_token_age_reads_only_the_timestamp(tmp_path):
    db = _tokens_db(tmp_path / "t.db", datetime.now(timezone.utc) - timedelta(days=3))
    assert sw.refresh_token_age_days(db) == pytest.approx(3.0, abs=0.01)
    assert sw.refresh_token_age_days(tmp_path / "missing.db") is None


def test_four_hour_bars_are_built_from_30_minute_candles(tmp_path):
    class C(_Client):
        def price_history(self, symbol, **kw):
            assert kw["frequency"] == 30
            t0 = pd.Timestamp("2026-01-05 14:30", tz="UTC")
            return _Resp(200, {"candles": [{"datetime": int((t0 + pd.Timedelta(minutes=30 * i)).timestamp() * 1000),
                                            "open": 10 + i, "high": 11 + i, "low": 9, "close": 10 + i, "volume": 100}
                                           for i in range(8)]})
    df = _feed(tmp_path, C()).get_bars_range("AAA", "4Hour", date(2026, 1, 5), date(2026, 1, 5))
    assert len(df) == 2 and df["volume"].sum() == 800 and df["high"].max() == 18
