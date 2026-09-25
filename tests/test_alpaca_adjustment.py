"""History from Alpaca must be split-adjusted, and cached apart from raw bars."""
from datetime import date
from unittest.mock import MagicMock

import pandas as pd
from alpaca.data.enums import Adjustment

from scanner.data import alpaca as A


def test_history_requests_are_split_adjusted(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    feed = A.AlpacaFeed(cache_dir=tmp_path / "d", intraday_cache_dir=tmp_path / "m")
    feed._client = MagicMock()
    feed._client.get_stock_bars.return_value = MagicMock(df=pd.DataFrame())
    feed.get_historical_daily("AAA", date(2026, 1, 5), date(2026, 1, 20))
    req = feed._client.get_stock_bars.call_args.args[0]
    assert req.adjustment == Adjustment.SPLIT


def test_the_cache_folders_name_the_adjustment():
    assert A._DEFAULT_DAILY_CACHE.name == "daily_split" and A._DEFAULT_INTRADAY_CACHE.name == "5m_split"


# ── intraday cache is keyed by bar size, and heals a wrongly sized file ───────

def test_intraday_cache_dir_depends_on_timeframe(tmp_path):
    from scanner.data.alpaca import AlpacaFeed
    f = AlpacaFeed.__new__(AlpacaFeed)
    f._intraday_cache_dir = tmp_path / "5m_split"
    f._intraday_cache_dir.mkdir()
    assert f._cache_dir_for("5Min") == tmp_path / "5m_split"
    assert f._cache_dir_for("1Min") == tmp_path / "1m_split"          # a sibling, never the same folder
    assert f._cache_dir_for("15Min") == tmp_path / "15m_split"


def test_one_minute_bars_are_not_accepted_as_a_five_minute_cache():
    import pandas as pd
    from scanner.data.alpaca import AlpacaFeed
    one = pd.DataFrame({"volume": 1.0}, index=pd.date_range("2026-09-22 08:00", periods=10, freq="1min", tz="UTC"))
    five = pd.DataFrame({"volume": 1.0}, index=pd.date_range("2026-09-22 08:00", periods=10, freq="5min", tz="UTC"))
    assert not AlpacaFeed._looks_like(one, "5Min")
    assert AlpacaFeed._looks_like(five, "5Min")
    assert AlpacaFeed._looks_like(five, "1Min") is True                 # sparse 1-min data is still 1-min data
