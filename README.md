# Binance Futures Squeeze Scanner — v2

Real-time short-squeeze detector for Binance USDT-M perpetuals with a
**STARTING → ACTIVE → EXHAUSTED → FAILED** state machine, adaptive per-coin
baselines, and an **evaluation database** that records what actually happens
after each signal.

> **Alert / research only. This project never places orders and needs no API
> keys with trading permissions.** A high score means the market currently
> *resembles* a squeeze — it is **not** a prediction. The point of the
> evaluation DB is to measure, over time, whether the signals mean anything.

---

## What it does

- Streams price, short/long liquidations, funding and volume for all ~900 USDT
  perps over Binance public WebSockets; polls open interest via REST.
- Scores each symbol on two 0–10 scales:
  - **START score** — price accelerating up, OI falling while price rises,
    short-liquidation burst, negative funding, aggressive taker buying, volume burst.
  - **EXHAUST score** — OI collapsed from the pre-squeeze baseline, large rapid
    gain, extreme liquidation multiple, volume spike, momentum fading.
- Runs a per-symbol **state machine**:

  ```
  NORMAL ──START≥7──▶ STARTING ──price↑ & OI↓ & liq↑ & vol↑──▶ ACTIVE ──EXHAUST≥7──▶ EXHAUSTED ──normalizes──▶ NORMAL
                          └── momentum dies / 10 min ──▶ FAILED ──▶ NORMAL
  ```

- **Remembers the pre-squeeze state**: when STARTING fires it locks in the
  baseline price and OI, so "OI −8% from start" is measured against the real
  starting point, not the last minute.
- **Adaptive baselines**: liquidations and volume are measured as a *multiple of
  that coin's own rolling average* (e.g. "5× normal"), so BTC and a thin altcoin
  are judged on the same footing.
- **Evaluation DB** (`events` table): every STARTING event records forward
  returns at +1/3/5/15/30/60 min, max favorable/adverse move, whether it reached
  ACTIVE, and its final phase. This is the honest test of the detector.
- Optional **Telegram alerts** on every phase transition.
- Self-contained **HTML report** (`reports/squeeze.html`) — live phases + the
  evaluation stats — that auto-refreshes in the browser. No server needed.

## Files

| file | purpose |
|---|---|
| `scanner_v2.py` | the v2 scanner (state machine, adaptive baselines, eval DB) |
| `squeeze_report.py` | renders `squeeze.db` → `reports/squeeze.html` |
| `keep.sh` | keeper: keeps the scanner alive + regenerates the report every 60 s |
| `config.json` | thresholds, alert level, DB path, Telegram toggle |
| `scanner.py` | original v1 scanner (kept for reference) |
| `dashboard.py` | original optional Streamlit dashboard (needs extra deps) |
| `evaluate.py` | ad-hoc forward-return summary over stored events |

## Requirements

Python 3.11+.

- **Scanner only** (what you need): `pip install aiohttp websockets`
- **Original Streamlit dashboard** (optional): `pip install -r requirements.txt`
  (adds pandas, numpy, streamlit, plotly)

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install aiohttp websockets

# 1) run the scanner (streams + scores, writes squeeze.db)
.venv/bin/python scanner_v2.py

# 2) in another shell, render the HTML report
.venv/bin/python squeeze_report.py
#    then open reports/squeeze.html in a browser
```

### Run it unattended (recommended)

`keep.sh` keeps the scanner alive and regenerates the report every 60 s, guarded
by a single-instance lock:

```bash
./keep.sh &
```

Keep it running across reboots with cron:

```cron
*/5 * * * * cd /path/to/SqueezeScanner && setsid nohup ./keep.sh >/dev/null 2>&1 &
@reboot sleep 70; cd /path/to/SqueezeScanner && setsid nohup ./keep.sh >/dev/null 2>&1 &
```

The report auto-refreshes every 60 s; the scanner scores every 10 s and needs
~4–5 minutes of 1-minute candles to warm up before it can score anything.

## Configuration (`config.json`)

```json
{
  "scan_all_usdt_perpetuals": true,
  "kline_interval": "1m",
  "score_alert_threshold": 7,
  "repeat_alert_minutes": 30,
  "telegram_enabled": true,
  "db_path": "squeeze.db"
}
```

Set `"symbols": ["BTCUSDT","ETHUSDT"]` to watch a fixed list instead of all perps.

## Telegram (optional)

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

Without these, the scanner still runs and stores everything locally.

## Not containerized

This runs directly on the host (venv + keeper + cron). There is no Dockerfile.
Containerizing is straightforward if wanted — a slim Python image with
`aiohttp`/`websockets`, `CMD ["python","scanner_v2.py"]`, and a bind-mount for
`squeeze.db` — but it is not required to run.

## Rate limits — important

The scanner uses public WebSockets for live data, but polls `openInterest` and
`exchangeInfo` over REST. **Do not run many instances at once** — repeated
`exchangeInfo` calls will get your IP temporarily banned (HTTP 418) by Binance.
`get_symbols` retries with backoff to survive a ban; keep to a single instance.

## Disclaimer

For education and research only. Not financial advice. A high score is a
resemblance to a historical squeeze pattern, not a forecast. Validate against the
evaluation database before trusting any signal.
