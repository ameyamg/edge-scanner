#!/usr/bin/env python
"""Build the scanner universe using a filter-based screener.

Filters applied (all thresholds are tunable via CLI flags):
  1. Active US equity assets from Alpaca (tradable only)
  2. Name-based ETF/fund/leveraged pre-filter
  3. Price >= $15  (Alpaca snapshot)
  4. 20-day avg volume >= 5,000,000 shares/day  (Alpaca daily bars)
  5. 20-day avg dollar volume >= $50M/day  (avg_vol * price)
  6. ATR% (20-day avg daily range / price) >= 1%  (no upper ceiling — higher is better)

Dollar volume (avg_vol × price) serves as the liquidity / size proxy: a stock
with $50M+ daily dollar volume is inherently large/mid-cap and actively traded.
This is a direct liquidity measure — more useful for a trading scanner than market
cap — and avoids any external API dependency beyond Alpaca.

Usage:
    python scripts/build_universe.py
    python scripts/build_universe.py --min-dollar-vol-m 100 --max-atr-pct 8.0
    python scripts/build_universe.py --out data/universe.csv --log-level DEBUG
"""
import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv(override=True)

log = logging.getLogger(__name__)

_ALPACA_LIVE_BASE  = "https://api.alpaca.markets"
_ALPACA_PAPER_BASE = "https://paper-api.alpaca.markets"
_DEFAULT_OUT       = Path("data/universe.csv")
_MIN_SYMBOLS       = 100

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Name fragments that indicate non-equity products (case-insensitive)
_ETF_NAME_FRAGMENTS = [
    "etf", " fund", "trust", "bull", "bear", "2x", "3x", "ultra",
    "inverse", "leveraged", "proshares", "direxion", "ishares",
    "vanguard etf", "spdr", "invesco qqq",
]


# ── Step 1: Alpaca asset list ──────────────────────────────────────────────────

def _fetch_alpaca_assets() -> list[dict]:
    """Return all active US equity assets from Alpaca.

    Tries the live endpoint first; falls back to paper-api on 401/403
    (paper-account credentials only work against paper-api.alpaca.markets).
    """
    api_key    = os.environ["ALPACA_API_KEY"]
    secret_key = os.environ["ALPACA_SECRET_KEY"]
    headers    = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
    params     = {"status": "active", "asset_class": "us_equity"}

    for base in (_ALPACA_LIVE_BASE, _ALPACA_PAPER_BASE):
        url  = f"{base}/v2/assets"
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code in (401, 403):
            log.debug("Assets endpoint %s returned %d — trying next", base, resp.status_code)
            continue
        resp.raise_for_status()
        assets = resp.json()
        log.info("Alpaca assets (%s): %d total active US equities", base, len(assets))
        return assets

    raise RuntimeError(
        "Could not authenticate with Alpaca trading API (tried live and paper endpoints). "
        "Check ALPACA_API_KEY / ALPACA_SECRET_KEY in .env."
    )


# ── Step 2: Name-based pre-filter ─────────────────────────────────────────────

def _name_filter(assets: list[dict]) -> list[str]:
    """Keep tradable symbols whose names don't look like ETFs/funds/leveraged."""
    kept = []
    for a in assets:
        if not a.get("tradable"):
            continue
        name = (a.get("name") or "").lower()
        if any(frag in name for frag in _ETF_NAME_FRAGMENTS):
            continue
        sym = a.get("symbol", "")
        if not sym or not all(c.isalpha() or c.isdigit() or c == "-" for c in sym):
            continue
        kept.append(sym)
    log.info("After name/tradable filter: %d symbols", len(kept))
    return kept


# ── Step 3: Price filter via Alpaca snapshot ──────────────────────────────────

def _price_filter(symbols: list[str], min_price: float) -> list[str]:
    """Keep symbols with latest trade price >= min_price."""
    from scanner.data.alpaca import market_data_feed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockSnapshotRequest

    api_key    = os.environ["ALPACA_API_KEY"]
    secret_key = os.environ["ALPACA_SECRET_KEY"]
    client     = StockHistoricalDataClient(api_key, secret_key)

    _BATCH = 500
    passed = []
    for i in range(0, len(symbols), _BATCH):
        batch = symbols[i : i + _BATCH]
        try:
            req   = StockSnapshotRequest(symbol_or_symbols=batch, feed=market_data_feed())
            snaps = client.get_stock_snapshot(req)
            for sym, snap in snaps.items():
                try:
                    price = float(snap.latest_trade.price) if snap.latest_trade else None
                except (AttributeError, TypeError, ValueError):
                    price = None
                if price is not None and price >= min_price:
                    passed.append(sym)
        except Exception as exc:
            log.warning("Snapshot batch %d error: %s — skipping batch", i // _BATCH, exc)
        log.debug("Price filter batch %d/%d: %d passed so far",
                  i // _BATCH + 1, (len(symbols) + _BATCH - 1) // _BATCH, len(passed))

    log.info("After price filter (>= $%.2f): %d symbols", min_price, len(passed))
    return passed


# ── Steps 4-6: Daily bars — avg volume, dollar volume, ATR% ──────────────────

def _compute_bar_metrics(symbols: list[str], days: int) -> dict[str, dict]:
    """Fetch daily bars and compute avg_vol, avg_dollar_vol, atr_pct, last_price."""
    from scanner.data.alpaca import market_data_feed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    api_key    = os.environ["ALPACA_API_KEY"]
    secret_key = os.environ["ALPACA_SECRET_KEY"]
    client     = StockHistoricalDataClient(api_key, secret_key)

    today  = date.today()
    end    = today - timedelta(days=1)
    # 2× window guarantees `days` trading bars despite weekends/holidays
    start  = today - timedelta(days=days * 2 + 10)

    _BATCH  = 1000
    metrics: dict[str, dict] = {}

    for i in range(0, len(symbols), _BATCH):
        batch = symbols[i : i + _BATCH]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=datetime.combine(start, datetime.min.time()),
                # end of day, not midnight — daily bars are stamped 04:00 UTC,
                # so a midnight-UTC cutoff drops the `end` date's bar
                end=datetime.combine(end, datetime.max.time()),
                feed=market_data_feed(),
            )
            bars = client.get_stock_bars(req)
            df   = bars.df  # MultiIndex: (symbol, timestamp)

            if df.empty:
                continue

            for sym in batch:
                try:
                    sym_df = (
                        df.xs(sym, level="symbol")
                        if isinstance(df.index, pd.MultiIndex)
                        else df
                    )
                    sym_df = sym_df.sort_index().tail(days)
                    if len(sym_df) < max(5, days // 2):
                        continue  # not enough history
                    last_close  = float(sym_df["close"].iloc[-1])
                    if last_close <= 0:
                        continue
                    avg_vol      = float(sym_df["volume"].mean())
                    avg_range    = float((sym_df["high"] - sym_df["low"]).mean())
                    avg_dvol     = avg_vol * last_close
                    atr_pct      = avg_range / last_close * 100
                    metrics[sym] = {
                        "avg_vol_20d":        round(avg_vol),
                        "avg_dollar_vol_20d": round(avg_dvol),
                        "atr_pct":            round(atr_pct, 2),
                        "last_price":         round(last_close, 2),
                    }
                except (KeyError, IndexError):
                    pass  # symbol absent from this batch response
        except Exception as exc:
            log.warning("Bars batch %d error: %s — skipping batch", i // _BATCH, exc)

        log.debug("Bar metrics batch %d/%d done",
                  i // _BATCH + 1, (len(symbols) + _BATCH - 1) // _BATCH)

    log.info("Bar metrics computed for %d / %d symbols", len(metrics), len(symbols))
    return metrics


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Build scanner universe CSV (filter-based)")
    parser.add_argument("--out",               default=str(_DEFAULT_OUT))
    parser.add_argument("--min-price",         type=float, default=15.0,
                        help="Minimum last trade price (default: 15.0)")
    parser.add_argument("--min-avg-vol",       type=int,   default=5_000_000,
                        help="Minimum 20-day avg daily share volume (default: 5000000)")
    parser.add_argument("--min-dollar-vol-m",  type=float, default=50.0,
                        help="Minimum 20-day avg daily dollar volume in millions (default: 50)")
    parser.add_argument("--min-atr-pct",       type=float, default=1.0,
                        help="Minimum ATR%% (default: 1.0)")
    parser.add_argument("--days",              type=int,   default=20,
                        help="Trading days of history for volume/ATR (default: 20)")
    parser.add_argument("--log-level",         default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    min_dollar_vol = args.min_dollar_vol_m * 1e6

    # Step 1+2: asset list + name pre-filter
    assets  = _fetch_alpaca_assets()
    symbols = _name_filter(assets)

    # Step 3: price filter
    symbols = _price_filter(symbols, args.min_price)
    if not symbols:
        log.error("No symbols passed the price filter — check Alpaca credentials and data access (ALPACA_FEED)")
        sys.exit(1)

    # Steps 4-6: bar metrics + apply volume / dollar-vol / ATR filters
    bar_metrics = _compute_bar_metrics(symbols, args.days)

    rows = []
    n_vol = n_dvol = n_atr = 0
    for sym in symbols:
        m = bar_metrics.get(sym)
        if m is None:
            continue
        if m["avg_vol_20d"] < args.min_avg_vol:
            n_vol += 1
            continue
        if m["avg_dollar_vol_20d"] < min_dollar_vol:
            n_dvol += 1
            continue
        if m["atr_pct"] < args.min_atr_pct:
            n_atr += 1
            continue
        rows.append({
            "symbol":             sym,
            "last_price":         m["last_price"],
            "avg_vol_20d":        m["avg_vol_20d"],
            "avg_dollar_vol_20d": m["avg_dollar_vol_20d"],
            "atr_pct":            m["atr_pct"],
        })

    log.info(
        "Filter rejects — avg_vol: %d  dollar_vol: %d  atr_pct: %d",
        n_vol, n_dvol, n_atr,
    )

    if len(rows) < _MIN_SYMBOLS:
        log.error(
            "Universe too small: %d symbols (minimum %d). "
            "Check network access and loosen filters.",
            len(rows), _MIN_SYMBOLS,
        )
        sys.exit(1)

    df = (
        pd.DataFrame(rows)
        .sort_values("avg_dollar_vol_20d", ascending=False)
        .reset_index(drop=True)
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    print(f"\nUniverse written to {out_path}  ({len(df)} symbols)")
    print(f"  Price >= ${args.min_price:.0f}  |  "
          f"Avg vol >= {args.min_avg_vol:,}  |  "
          f"Dollar vol >= ${args.min_dollar_vol_m:.0f}M/day  |  "
          f"ATR% >= {args.min_atr_pct:.0f}%")
    print(f"  Most liquid (by dollar vol): {', '.join(df['symbol'].head(10).tolist())}")


if __name__ == "__main__":
    main()
