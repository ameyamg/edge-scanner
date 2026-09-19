# Edge Scanner

A real-time intraday stock scanner for US equities, with a browser dashboard you lay out
yourself. It streams 1-minute bars for thousands of symbols from Alpaca, evaluates the setups
you define on every bar, and publishes alerts to the dashboard and to a WebSocket feed that
other programs can subscribe to.

It ships with sample setups, a library of scan ideas and sample universe filters, all
editable, so you can start from something that works and build your own.

> Educational software. Not financial advice. No warranty. You are responsible for your own
> trading decisions.

## What it does

- **Custom setups, no code.** Compose a setup in the dashboard from a catalog of about 45
  triggers (candle patterns, level breaks, crosses, VWAP and EMA behaviour, opening range,
  momentum, relative strength vs SPY), add conditions it must meet, and pick the universe it
  runs on. Changes apply on the next bar, no restart.
- **Universe filters.** Named screens (price, liquidity, ATR, float, sector, relative volume
  and more) that decide which stocks a setup may alert on.
- **Dashboard.** Free-floating windows: alert tables, charts, rankings (top gainers, losers,
  most active, etc.), news, stock info, watchlists, a per-symbol setup check that explains why
  a setup did or did not fire, and a market clock. Multiple saved screens.
- **One data connection.** Everything runs in one process on one Alpaca market-data websocket.
- **A feed for other programs.** `ws://localhost:7777/ws/alerts`, with server-side filters.
- **News.** Alpaca (Benzinga) news plus free per-symbol Yahoo Finance and Nasdaq feeds.
- **Extensible.** Optional engine plugins can add built-in setups and data providers
  (`scanner/plugins.py`).

## Requirements

- Python 3.11+ and Node.js 20+ (to build the dashboard)
- An [Alpaca](https://alpaca.markets) account (paper is fine) and API keys
- Market data: the default `ALPACA_FEED=sip` needs a paid Alpaca market-data plan. The free
  `ALPACA_FEED=iex` works, but it is one exchange, so volume and relative volume read far
  lower and volume-based setups fire much less.

Tested on Windows; macOS and Linux should work (`start_scanner.sh`).

## Quick start

```bash
git clone https://github.com/simonro/edge-scanner.git
cd edge-scanner
python -m venv .venv
```

Activate the venv (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate`
elsewhere), then:

```bash
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` and `ALPACA_FEED`.

Build the dashboard once:

```bash
npm --prefix dashboard-v2 install
npm --prefix dashboard-v2 run build
```

Optional, install the setup library:

```bash
python scripts/install_setup_library.py
```

Run it (`start_scanner.bat` on Windows):

```bash
./start_scanner.sh
```

Open http://localhost:7777. The first start downloads a year of daily bars and 20 days of
5-minute bars for the whole universe and can take 10 to 20 minutes; later starts use the cache
in `data/`.

The full guide is [USER_GUIDE.md](USER_GUIDE.md).

## Layout

| Path | What |
|---|---|
| `scanner/` | the engine: data feed, per-symbol state, indicators, setups, universe filters, API |
| `scanner/trigger_catalog.py` | every trigger a custom setup can use |
| `scanner/conditions.py` | the conditions setups and universe filters can check |
| `scanner/custom_setups_defaults.json` | sample setups, seeded on first run |
| `scanner/setup_library.json` | the setup library (`scripts/install_setup_library.py`) |
| `scanner/universe_profiles_defaults.json` | sample universe filters |
| `scanner/plugins.py` | extension points for optional engine plugins |
| `scripts/run_live.py` | the live scanner |
| `dashboard-v2/` | the React dashboard |
| `data/` | runtime data: caches, settings, your setups, alert archives (gitignored) |

## Development

```bash
python -m pytest
npm --prefix dashboard-v2 run dev
```

`npm run dev` serves the dashboard with hot reload on http://localhost:5174 and proxies the API
to a running scanner on 7777.

## License

MIT, see [LICENSE](LICENSE). Third-party notices, including the TradingView Lightweight Charts
attribution, are in [NOTICE](NOTICE).
