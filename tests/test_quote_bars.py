"""1-minute bars built from quotes (scanner/data/quote_bars.py)."""
import pandas as pd

from scanner.data.quote_bars import QuoteBarBuilder


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _builder(start: float = 600_000 * 60.0):
    bars, clock = [], Clock(start)
    b = QuoteBarBuilder(bars.append, clock)
    b.track("AAPL", grace=3.0)
    return b, bars, clock


def test_bar_is_built_from_trades_inside_the_minute():
    b, bars, clock = _builder()
    b.on_quote("AAPL", last=100.0, total_volume=1_000)          # baseline: not a trade
    clock.t += 5;  b.on_quote("AAPL", last=100.5, total_volume=1_200)
    clock.t += 5;  b.on_quote("AAPL", last=99.8, total_volume=1_500)
    clock.t += 5;  b.on_quote("AAPL", last=100.1, total_volume=1_600)
    assert bars == []                                            # the minute is still open
    clock.t += 60
    assert b.flush() == 1
    bar = bars[0]
    assert (bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]) == (100.5, 100.5, 99.8, 100.1, 600)
    assert bar["timestamp"] == pd.Timestamp(600_000 * 60, unit="s", tz="UTC")     # start of the minute, UTC
    assert bar["symbol"] == "AAPL" and bar["source"] == "quotes"


def test_no_trade_means_no_bar():
    b, bars, clock = _builder()
    b.on_quote("AAPL", last=100.0, total_volume=1_000)
    clock.t += 10; b.on_quote("AAPL", last=100.0, total_volume=1_000)   # bid/ask moved, nothing traded
    clock.t += 120
    assert b.flush() == 0 and bars == []


def test_partial_updates_keep_the_last_known_price():
    """A quote stream sends only the fields that changed."""
    b, bars, clock = _builder()
    b.on_quote("AAPL", last=50.0, total_volume=100)
    clock.t += 1; b.on_quote("AAPL", total_volume=150)           # volume only
    clock.t += 1; b.on_quote("AAPL", last=50.2)                  # price only: no trade yet
    clock.t += 1; b.on_quote("AAPL", total_volume=175)
    clock.t += 70; b.flush()
    assert (bars[0]["open"], bars[0]["close"], bars[0]["volume"]) == (50.0, 50.2, 75)


def test_grace_holds_the_bar_back_until_late_quotes_can_arrive():
    b, bars, clock = _builder()
    b.track("SLOW", grace=15.0)
    b.on_quote("SLOW", last=10.0, total_volume=100)
    clock.t += 30; b.on_quote("SLOW", last=10.1, total_volume=200)
    clock.t = 600_001 * 60.0 + 10                                # 10s into the next minute
    assert b.flush() == 0
    clock.t += 6
    assert b.flush() == 1


def test_a_quote_in_a_new_minute_closes_the_previous_bar_first():
    b, bars, clock = _builder()
    b.on_quote("AAPL", last=100.0, total_volume=1_000)
    clock.t += 10; b.on_quote("AAPL", last=101.0, total_volume=1_100)
    clock.t += 60; b.on_quote("AAPL", last=102.0, total_volume=1_300)
    assert len(bars) == 1 and bars[0]["close"] == 101.0 and bars[0]["volume"] == 100
    clock.t += 70; b.flush()
    assert bars[1]["open"] == 102.0 and bars[1]["volume"] == 200


def test_a_new_high_of_day_is_exact_even_if_no_quote_caught_it():
    """Polled quotes miss a spike, but the quote's own day high moved: use it."""
    b, bars, clock = _builder()
    b.on_quote("AAPL", last=50.00, total_volume=1_000, day_high=50.10, day_low=49.00)
    clock.t += 10
    b.on_quote("AAPL", last=50.10, total_volume=2_000, day_high=50.40, day_low=49.00)   # spiked to 50.40 between polls
    clock.t += 70; b.flush()
    assert bars[0]["high"] == 50.40 and bars[0]["close"] == 50.10


def test_an_unchanged_day_high_is_not_written_into_the_bar():
    b, bars, clock = _builder()
    b.on_quote("AAPL", last=50.0, total_volume=1_000, day_high=55.0, day_low=45.0)
    clock.t += 10; b.on_quote("AAPL", last=50.1, total_volume=1_100, day_high=55.0, day_low=45.0)
    clock.t += 70; b.flush()
    assert bars[0]["high"] == 50.1 and bars[0]["low"] == 50.1


def test_provider_volume_reset_counts_volume_since_the_reset():
    b, bars, clock = _builder()
    b.on_quote("AAPL", last=50.0, total_volume=900_000)
    clock.t += 10; b.on_quote("AAPL", last=50.5, total_volume=300)     # counter restarted
    clock.t += 70; b.flush()
    assert bars[0]["volume"] == 300


def test_untracked_symbols_are_ignored():
    b, bars, clock = _builder()
    b.on_quote("ZZZ", last=1.0, total_volume=1)
    b.on_quote("ZZZ", last=1.1, total_volume=5)
    clock.t += 120
    assert b.flush() == 0
