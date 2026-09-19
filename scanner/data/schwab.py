"""DataFeed backed by the Charles Schwab API (via the schwabdev wrapper).

Selected with DATA_PROVIDER=schwab in .env (or run_live.py --feed schwab);
AlpacaFeed remains the default. Needs `pip install schwabdev` and a one-time
login with scripts/schwab_auth.py (repeat weekly: Schwab expires it after 7 days).

Why this exists: Alpaca allows ONE concurrent market-data websocket per
account, and costs $99/mo. Schwab is free with a brokerage account and gives
an independent stream. Whether the DATA is equivalent is a separate question,
answered by scripts/check_schwab_match.py, not by this file.

Known differences from AlpacaFeed, all deliberate:
  * Cache lives under data/schwab/** so it can never mix with Alpaca's parquet
    cache. Mixing them would silently corrupt the live scanner's history.
  * Schwab's price-history endpoint is ONE SYMBOL PER REQUEST (no batch
    variant), so multi-symbol fetches are threaded and throttled instead of
    batched. Expect slower cold warmups.
  * Schwab returns no vwap / trade_count on candles; those columns are present
    but NaN so the DataFrame schema still matches AlpacaFeed.
  * Minute history has a shorter lookback than Alpaca's. Verify empirically
    with check_schwab_match.py before relying on it for the 20-day RVOL profile.
  * The refresh token expires every 7 days and re-auth opens a browser
    (Schwab's rule, not schwabdev's). Unattended runs WILL eventually stop.

Credentials (add to .env yourself, never commit):
    SCHWAB_APP_KEY=...
    SCHWAB_APP_SECRET=...
    SCHWAB_CALLBACK_URL=https://127.0.0.1
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

from scanner.cache import parquet
from scanner.data.interface import DataFeed, Timeframe

load_dotenv(override=True)

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Isolated from Alpaca's data/daily + data/5m. Do NOT point these at the Alpaca
# dirs: the two providers disagree on volume and would poison the live cache.
_DEFAULT_DAILY_CACHE    = Path("data/schwab/daily")
_DEFAULT_INTRADAY_CACHE = Path("data/schwab/5m")

_BAR_COLS = ["open", "high", "low", "close", "volume", "vwap", "trade_count"]

# (periodType, frequencyType, frequency). periodType is NOT optional: Schwab
# defaults it to "day", and "day" only permits frequencyType="minute", so
# asking for daily candles without it returns 400 Bad Request. Valid pairs:
#     day -> minute        month -> daily, weekly
#     year -> daily, weekly, monthly     ytd -> daily, weekly
# Minute frequencies are limited to 1, 5, 10, 15, 30 (no 60), so 1Hour is
# resampled from 30-minute candles.
_FREQ: dict[str, tuple[str, str, int]] = {
    "1Min":  ("day",  "minute", 1),
    "5Min":  ("day",  "minute", 5),
    "15Min": ("day",  "minute", 15),
    "30Min": ("day",  "minute", 30),
    "Day":   ("year", "daily",  1),
    "1Week": ("year", "weekly", 1),
}

_MAX_WORKERS = 4      # concurrency; the RATE is set by _LIMITER, not by this

# Schwab's market-data API allows about 120 requests a minute per app. Every
# request in this module goes through one shared limiter so that threads
# together stay under it. Override with SCHWAB_MAX_RPM if Schwab raises yours.
_MAX_RPM = float(os.environ.get("SCHWAB_MAX_RPM", "110"))
_RETRIES = 4                 # on HTTP 429 / 5xx, with exponential backoff
_REFRESH_TOKEN_DAYS = 7      # Schwab's rule; after this a browser login is required
_TOKENS_DB = Path(os.path.expanduser("~/.schwabdev/tokens.db"))


class _RateLimiter:
    """Evenly spaced, thread-safe: at most `rpm` acquisitions per minute."""

    def __init__(self, rpm: float, clock=time.monotonic, sleep=time.sleep) -> None:
        self._interval = 60.0 / max(rpm, 1.0)
        self._next = 0.0
        self._lock = threading.Lock()
        self._clock, self._sleep = clock, sleep

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            wait = self._next - now
            self._next = max(now, self._next) + self._interval
        if wait > 0:
            self._sleep(wait)


_LIMITER = _RateLimiter(_MAX_RPM)


def refresh_token_age_days(db: Path = _TOKENS_DB) -> float | None:
    """Days since Schwab last issued the refresh token, or None if unknown.
    Reads only the timestamp column, never a token."""
    try:
        import sqlite3
        with sqlite3.connect(db) as con:
            row = con.execute("select refresh_token_issued from schwabdev").fetchone()
        issued = datetime.fromisoformat(row[0])
        return (datetime.now(issued.tzinfo) - issued).total_seconds() / 86400
    except Exception:
        return None


def _covers(df: pd.DataFrame, start: date, slack_days: int = 6) -> bool:
    """True when a cached frame reaches back to `start` (allowing for weekends
    and holidays at the front of the range)."""
    if df is None or df.empty:
        return False
    first = pd.Timestamp(df.index.min()).tz_convert(_ET).date() if df.index.tz is not None \
        else pd.Timestamp(df.index.min()).date()
    return (first - start).days <= slack_days


class SchwabFeed(DataFeed):
    """DataFeed implementation over the Schwab market-data API."""

    def __init__(
        self,
        cache_dir: Path = _DEFAULT_DAILY_CACHE,
        intraday_cache_dir: Path = _DEFAULT_INTRADAY_CACHE,
        client=None,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._intraday_cache_dir = Path(intraday_cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._intraday_cache_dir.mkdir(parents=True, exist_ok=True)

        if client is not None:
            self._client = client            # injected (tests)
        else:
            age = refresh_token_age_days()
            if age is not None and age >= _REFRESH_TOKEN_DAYS:
                # schwabdev would otherwise stop at a console prompt waiting
                # for a browser login, which hangs an unattended start.
                raise RuntimeError(
                    f"Schwab login expired ({age:.0f} days since the last login; Schwab allows "
                    f"{_REFRESH_TOKEN_DAYS}). Run: python scripts/schwab_auth.py")
            if age is not None and age >= _REFRESH_TOKEN_DAYS - 1:
                log.warning("Schwab login expires within a day; run scripts/schwab_auth.py soon")
            import schwabdev
            key    = os.environ.get("SCHWAB_APP_KEY")
            secret = os.environ.get("SCHWAB_APP_SECRET")
            if not key or not secret:
                raise RuntimeError(
                    "SCHWAB_APP_KEY / SCHWAB_APP_SECRET missing from .env. "
                    "Register an app at developer.schwab.com, then add them."
                )
            self._client = schwabdev.Client(
                app_key=key,
                app_secret=secret,
                callback_url=os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1"),
            )
        self._stream = None
        self._stop_evt = threading.Event()

    # ── Candle parsing ────────────────────────────────────────────────────────

    @staticmethod
    def _candles_to_df(payload: dict) -> pd.DataFrame:
        """Schwab candle JSON -> UTC-indexed OHLCV frame matching AlpacaFeed."""
        candles = (payload or {}).get("candles") or []
        if not candles:
            return pd.DataFrame(columns=_BAR_COLS).rename_axis("timestamp")
        df = pd.DataFrame(candles)
        df["timestamp"] = pd.to_datetime(df["datetime"], unit="ms", utc=True)
        df = df.set_index("timestamp").sort_index()
        # Schwab gives no vwap / trade_count; keep the columns so downstream code
        # that reindexes on _BAR_COLS behaves identically to Alpaca.
        for col in ("vwap", "trade_count"):
            if col not in df.columns:
                df[col] = float("nan")
        return df.reindex(columns=_BAR_COLS)

    def _price_history(self, symbol: str, timeframe: Timeframe,
                       start: date, end: date) -> pd.DataFrame:
        if timeframe in ("1Hour", "4Hour"):
            # Schwab has no hourly candles: build them from 30-minute ones.
            half = self._price_history(symbol, "30Min", start, end)
            if half.empty:
                return half
            return half.resample("1h" if timeframe == "1Hour" else "4h").agg({
                "open": "first", "high": "max", "low": "min", "close": "last",
                "volume": "sum", "vwap": "mean", "trade_count": "sum",
            }).dropna(subset=["open"])

        ptype, ftype, freq = _FREQ[timeframe]
        # period is deliberately omitted: Schwab rejects it alongside
        # startDate/endDate, which is how this feed always queries.
        resp = self._request(lambda: self._client.price_history(
            symbol=symbol,
            periodType=ptype,
            frequencyType=ftype,
            frequency=freq,
            startDate=datetime.combine(start, datetime.min.time()),
            endDate=datetime.combine(end, datetime.max.time()),
            needExtendedHoursData=(ftype == "minute"),   # premarket levels need it
        ))
        return self._candles_to_df(resp.json())

    @staticmethod
    def _request(call):
        """Rate-limited call with retry on 429 and 5xx. Raises on anything else."""
        delay = 2.0
        for attempt in range(_RETRIES + 1):
            _LIMITER.acquire()
            resp = call()
            status = getattr(resp, "status_code", 200)
            if status == 429 or status >= 500:
                if attempt == _RETRIES:
                    resp.raise_for_status()
                log.debug("Schwab HTTP %s, retrying in %.0fs", status, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            resp.raise_for_status()
            return resp
        return resp

    # ── DataFeed interface ────────────────────────────────────────────────────

    def get_historical_daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        # Reuse today's file only if it reaches back far enough: the universe
        # build and the warmup ask for different spans on the same morning.
        p = self._cache_dir / f"{symbol}.parquet"
        if p.exists() and datetime.fromtimestamp(p.stat().st_mtime).date() >= date.today():
            cached = parquet.load(symbol, self._cache_dir)
            if _covers(cached, start):
                return cached
        df = self._price_history(symbol, "Day", start, end)
        parquet.save(symbol, df, self._cache_dir)
        return df

    def get_historical_bars(self, symbol: str, timeframe: Timeframe,
                            start: date, end: date) -> pd.DataFrame:
        p = self._intraday_cache_dir / f"{symbol}.parquet"
        if p.exists() and datetime.fromtimestamp(p.stat().st_mtime).date() >= date.today():
            cached = parquet.load(symbol, self._intraday_cache_dir)
            if _covers(cached, start):
                return cached
        df = self._price_history(symbol, timeframe, start, end)
        parquet.save(symbol, df, self._intraday_cache_dir)
        return df

    def get_bars_range(self, symbol: str, timeframe: Timeframe,
                       start: date, end: date) -> pd.DataFrame:
        """Uncached range fetch (used by the chart API)."""
        return self._price_history(symbol, timeframe, start, end)

    def get_todays_bars(self, symbol: str, timeframe: Timeframe) -> pd.DataFrame:
        today = datetime.now(_ET).date()
        return self._price_history(symbol, timeframe, today, today)

    def get_todays_bars_multi(self, symbols: list[str],
                              timeframe: Timeframe = "1Min") -> dict[str, pd.DataFrame]:
        """One request per symbol (Schwab has no batch endpoint), threaded."""
        today = datetime.now(_ET).date()
        out: dict[str, pd.DataFrame] = {}

        def _one(sym: str):
            return sym, self._price_history(sym, timeframe, today, today)

        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futs = [pool.submit(_one, s) for s in symbols]
            for fut in as_completed(futs):
                try:
                    sym, df = fut.result()
                    if not df.empty:
                        out[sym] = df[["open", "high", "low", "close", "volume"]]
                except Exception as exc:
                    log.warning("Schwab today's bars failed: %s", exc)
        return out

    def _multi(self, fetch, symbols: list[str], progress=None) -> dict[str, pd.DataFrame]:
        """Run fetch(symbol) for many symbols on a few threads. The shared rate
        limiter, not the thread count, sets the pace; failures are logged and
        skipped so one bad symbol never stops a warmup."""
        out: dict[str, pd.DataFrame] = {}
        done = 0
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futs = {pool.submit(fetch, s): s for s in symbols}
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    df = fut.result()
                    if df is not None and not df.empty:
                        out[sym] = df
                except Exception as exc:
                    log.warning("Schwab history failed for %s: %s", sym, exc)
                done += 1
                if progress is not None:
                    progress(done, len(symbols))
        return out

    def get_historical_daily_multi(self, symbols: list[str], start: date, end: date,
                                   workers: int = 0, progress=None) -> dict[str, pd.DataFrame]:
        """Daily bars for many symbols, one request each (Schwab has no batch
        endpoint): about len(symbols) / SCHWAB_MAX_RPM minutes when not cached."""
        return self._multi(lambda s: self.get_historical_daily(s, start, end), symbols, progress)

    def get_historical_bars_multi(self, symbols: list[str], timeframe: Timeframe,
                                  start: date, end: date, workers: int = 0,
                                  progress=None) -> dict[str, pd.DataFrame]:
        return self._multi(lambda s: self.get_historical_bars(s, timeframe, start, end), symbols, progress)

    def get_snapshot(self, symbols: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        _BATCH = 100   # quotes() IS batched, unlike price history
        for i in range(0, len(symbols), _BATCH):
            batch = symbols[i : i + _BATCH]
            try:
                resp = self._request(lambda b=batch: self._client.quotes(symbols=b, fields="quote"))
                for sym, payload in (resp.json() or {}).items():
                    q = (payload or {}).get("quote") or {}
                    result[sym] = {
                        "price":        q.get("lastPrice"),
                        "bid":          q.get("bidPrice"),
                        "ask":          q.get("askPrice"),
                        "daily_volume": q.get("totalVolume"),
                    }
            except Exception as exc:
                log.warning("Schwab quotes batch failed: %s", exc)
        return result

    # ── Streaming ─────────────────────────────────────────────────────────────

    @staticmethod
    def handle_message(raw, callback: Callable[[dict], None]) -> None:
        """Decode one raw streamer message and emit any CHART_EQUITY bars.

        Split out from subscribe_minute_bars so it can be tested without
        opening a socket. Field order is 0 key, 1 sequence, 2 open, 3 high,
        4 low, 5 close, 6 volume, 7 chart time (epoch ms), 8 chart day --
        this is schwabdev's CORRECTED order; Schwab's own docs are wrong.
        Malformed payloads are skipped rather than killing the stream.
        """
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (TypeError, ValueError):
            return
        for block in (msg or {}).get("data", []) or []:
            if block.get("service") != "CHART_EQUITY":
                continue
            for c in block.get("content", []) or []:
                try:
                    callback({
                        "symbol":    c.get("key"),
                        "timestamp": pd.to_datetime(int(c["7"]), unit="ms", utc=True),
                        "open":      float(c["2"]),
                        "high":      float(c["3"]),
                        "low":       float(c["4"]),
                        "close":     float(c["5"]),
                        "volume":    float(c["6"]),
                    })
                except (KeyError, TypeError, ValueError) as exc:
                    log.debug("Skipping malformed CHART_EQUITY payload: %s", exc)

    def subscribe_minute_bars(self, symbols: list[str],
                              callback: Callable[[dict], None]) -> None:
        """Stream 1-min OHLCV via CHART_EQUITY. Blocks until stop_stream().

        CHART_EQUITY field order (schwabdev translate.py, corrected against
        Schwab's own docs which are wrong): 0 key, 1 sequence, 2 open, 3 high,
        4 low, 5 close, 6 volume, 7 chart time (epoch ms), 8 chart day.
        """
        # schwabdev 4.0.0 exposes no Client.stream property; construct it.
        import schwabdev
        stream = schwabdev.Stream(self._client)
        self._stream = stream
        self._stop_evt.clear()

        def _receiver(raw) -> None:
            self.handle_message(raw, callback)

        stream.start(receiver=_receiver, daemon=True)
        # Subscribe in chunks; Schwab caps the key list per request.
        _CHUNK = 250
        for i in range(0, len(symbols), _CHUNK):
            stream.send(stream.chart_equity(symbols[i : i + _CHUNK], "0,1,2,3,4,5,6,7,8"))
        log.info("Schwab: subscribed to CHART_EQUITY for %d symbols", len(symbols))

        while not self._stop_evt.wait(1.0):
            pass

    def stop_stream(self) -> None:
        self._stop_evt.set()
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception as exc:
                log.debug("Schwab stream stop: %s", exc)
            log.info("Schwab stream stopped")
