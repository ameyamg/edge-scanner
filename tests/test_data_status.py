"""The dashboard's provider and data-age readout (/api/v2/clock "data")."""
import time
from types import SimpleNamespace

import pandas as pd

from scanner.api_v2 import data_status, provider_label
from tests.helpers import _bar, _state


def test_provider_label(monkeypatch):
    AlpacaFeed = type("AlpacaFeed", (), {})
    SchwabFeed = type("SchwabFeed", (), {})
    monkeypatch.setenv("ALPACA_FEED", "iex")
    assert provider_label(AlpacaFeed()) == "Alpaca IEX"
    monkeypatch.delenv("ALPACA_FEED")
    assert provider_label(AlpacaFeed()) == "Alpaca SIP"
    assert provider_label(SchwabFeed()) == "Schwab"
    assert provider_label(type("ReplayFeed", (), {})()) == "Replay"
    assert provider_label(type("X", (), {"PROVIDER_LABEL": "Demo"})()) == "Demo"
    assert provider_label(None) is None


def test_data_status_reports_the_newest_bar_and_its_age():
    from scanner.live_scanner import LiveScanner
    sc = LiveScanner.__new__(LiveScanner)
    sc.last_bar_ts, sc.last_bar_wall = None, None
    empty = data_status(SimpleNamespace(scanner=sc, feed=None))
    assert empty["last_bar_et"] is None and empty["last_bar_age_s"] is None
    sc.last_bar_ts = pd.Timestamp("2026-09-25 14:42", tz="UTC")
    sc.last_bar_wall = time.time() - 12
    d = data_status(SimpleNamespace(scanner=sc, feed=type("SchwabFeed", (), {})()))
    assert d["provider"] == "Schwab"
    assert d["last_bar_et"] == "2026-09-25T10:42:00-04:00"
    assert 11 <= d["last_bar_age_s"] <= 14


def test_live_scanner_records_the_newest_bar(tmp_path, monkeypatch):
    """Any bar that passes the day check advances it; an older bar does not rewind it."""
    from scanner.live_scanner import LiveScanner
    sc = LiveScanner.__new__(LiveScanner)
    sc.last_bar_ts, sc.last_bar_wall = None, None
    sc._spy_state, sc._states = None, {}
    monkeypatch.setattr(sc, "_roll_session_if_new_day", lambda bar: True, raising=False)
    sc._on_bar(_bar(100.0, et="2024-01-02 10:05", sym="AAA"))
    sc._on_bar(_bar(100.0, et="2024-01-02 10:03", sym="BBB"))
    assert sc.last_bar_ts.tz_convert("America/New_York").strftime("%H:%M") == "10:05"
    assert time.time() - sc.last_bar_wall < 5
