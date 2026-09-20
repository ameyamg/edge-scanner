"""1-minute bars built from quotes, for providers that cap their bar stream.

Schwab streams real 1-minute bars for at most 300 symbols per account, but
quotes for far more (3,000 on its level-one stream, 500 per REST request). A
quote carries the last trade price and the cumulative volume for the day, which
is enough to rebuild a minute bar:

    open / close   first and last trade price seen in the minute
    high / low     highest and lowest trade price SEEN in the minute
    volume         growth of the day's cumulative volume across the minute

How close that is to a real bar depends on how often quotes arrive. On a live
quote stream (several updates a second) the only thing lost is a spike that
appears and reverts between two updates. When quotes are polled every few
seconds, open, close and volume stay good and high / low are approximate. One
correction makes the case that matters most exact again: when the quote's own
high or low OF THE DAY moves, that extreme is known precisely, so a new high or
low of day is never missed however slowly the quotes arrive.

Bars follow the same rules as real ones: stamped at the start of their minute in
UTC, emitted once after the minute ends, and only for minutes that traded. A
minute with no trade produces no bar.

Thread-safe: quotes may arrive from a stream thread and a polling thread while
a timer thread flushes finished minutes.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd


@dataclass
class _Sym:
    last: Optional[float] = None          # last trade price seen
    total: Optional[float] = None         # cumulative day volume seen
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    minute: Optional[int] = None          # epoch minute of the bar being built
    o: float = 0.0
    h: float = 0.0
    l: float = 0.0
    c: float = 0.0
    v: float = 0.0
    grace: float = 3.0                    # seconds after the minute ends before it may be emitted


class QuoteBarBuilder:
    """Turns quote updates into 1-minute bars and hands them to `emit`.

    Args:
        emit:  called with one bar dict (symbol, timestamp, open, high, low,
               close, volume, source) per symbol per traded minute.
        clock: wall clock in epoch seconds; injectable for tests.
    """

    def __init__(self, emit: Callable[[dict], None], clock: Callable[[], float] = time.time) -> None:
        self._emit = emit
        self._clock = clock
        self._syms: dict[str, _Sym] = {}
        self._lock = threading.Lock()
        self.bars_emitted = 0
        self.quotes_seen = 0

    def track(self, symbol: str, grace: float) -> None:
        """Register a symbol. `grace` is how long after a minute ends its bar is
        held back: a little over the longest gap between two quotes for it."""
        with self._lock:
            self._syms.setdefault(symbol, _Sym()).grace = grace

    def on_quote(self, symbol: str, last: Optional[float] = None, total_volume: Optional[float] = None,
                 day_high: Optional[float] = None, day_low: Optional[float] = None) -> None:
        """One quote update. Any field may be missing (a stream sends only what
        changed); the last known value is kept."""
        done: Optional[dict] = None
        with self._lock:
            s = self._syms.get(symbol)
            if s is None:
                return
            self.quotes_seen += 1
            if last is not None and last > 0:
                s.last = float(last)
            new_high = day_high is not None and s.day_high is not None and day_high > s.day_high
            new_low = day_low is not None and s.day_low is not None and 0 < day_low < s.day_low
            if day_high is not None and day_high > 0:
                s.day_high = float(day_high)
            if day_low is not None and day_low > 0:
                s.day_low = float(day_low)

            if total_volume is None or s.last is None:
                return
            total = float(total_volume)
            if s.total is None:                       # first sight: a baseline, not a trade
                s.total = total
                return
            if total == s.total:
                return                                # bid/ask moved, nothing traded
            # A smaller total means the provider reset its day counter: everything
            # it now reports traded since the reset.
            delta = total - s.total if total > s.total else total
            s.total = total
            if delta <= 0:
                return

            minute = int(self._clock() // 60)
            if s.minute is not None and minute != s.minute:
                done = self._close(symbol, s)
            if s.minute is None:
                s.minute, s.o, s.h, s.l, s.v = minute, s.last, s.last, s.last, 0.0
            s.c = s.last
            s.h = max(s.h, s.last)
            s.l = min(s.l, s.last)
            s.v += delta
            # The day's extreme moved since the previous quote, so it was printed
            # inside this bar: take it exactly, even if no quote caught it.
            if new_high and s.day_high is not None and s.day_high >= s.h:
                s.h = s.day_high
            if new_low and s.day_low is not None and s.day_low <= s.l:
                s.l = s.day_low
        if done is not None:
            self._send(done)

    def flush(self) -> int:
        """Emit every bar whose minute has ended and whose grace has passed.
        Call about once a second. Returns how many bars were emitted."""
        now = self._clock()
        out: list[dict] = []
        with self._lock:
            for symbol, s in self._syms.items():
                if s.minute is not None and now >= (s.minute + 1) * 60 + s.grace:
                    out.append(self._close(symbol, s))
        for bar in out:
            self._send(bar)
        return len(out)

    def _close(self, symbol: str, s: _Sym) -> dict:
        bar = {
            "symbol": symbol,
            "timestamp": pd.Timestamp(s.minute * 60, unit="s", tz="UTC"),
            "open": s.o, "high": s.h, "low": s.l, "close": s.c, "volume": s.v,
            "source": "quotes",
        }
        s.minute = None
        return bar

    def _send(self, bar: dict) -> None:
        self.bars_emitted += 1
        self._emit(bar)
