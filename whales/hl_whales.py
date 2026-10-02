#!/usr/bin/env python3
"""
Hyperliquid whale-positioning ingester (the 'pumpglass' brick).

Hyperliquid is a decentralized perp exchange: every trader's positions, entry
price, unrealized PnL, leverage and liquidation price are ON-CHAIN and PUBLIC.
This is the data source real whale-trackers use (a CEX like Binance keeps these
private). Free, no API key.

What this does, each sweep:
  1. Pull the public leaderboard  -> ~46k addresses with account value + PnL.
  2. Keep the whales (accountValue >= WHALE_MIN_USD), top MAX_WHALES by size.
  3. For each, read its live positions (coin, size, entry, uPnL, liq, leverage).
  4. Store a timestamped snapshot in SQLite.

Snapshots over time = accumulation/distribution (diff two sweeps).

Rate-limit discipline (we learned this the hard way with Binance):
  - Hyperliquid /info shares ~1200 weight/min per IP; clearinghouseState is cheap.
  - We sleep between calls, sweep every POLL_SECONDS (default 5 min), back off on
    429, and never restart-thrash. One instance. See the repo memory rule.
"""
import json
import os
import sqlite3
import time
import urllib.request
import urllib.error

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

DB_PATH = os.environ.get("WHALE_DB_PATH", os.path.join(os.path.dirname(__file__), "whales.db"))
WHALE_MIN_USD = float(os.environ.get("WHALE_MIN_USD", "1000000"))   # $1M+ accounts
MAX_WHALES = int(os.environ.get("MAX_WHALES", "250"))               # top N by account value
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))          # sweep cadence
REQ_DELAY = float(os.environ.get("REQ_DELAY", "0.08"))            # gap between position calls


def _post(payload, tries=4):
    body = json.dumps(payload).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(INFO_URL, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 2 ** i
                print(f"  429 rate-limited, backing off {wait}s")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            time.sleep(1 + i)
    return None


def get_leaderboard():
    with urllib.request.urlopen(LEADERBOARD_URL, timeout=30) as r:
        data = json.load(r)
    rows = data["leaderboardRows"]

    def perf(row, window):
        for w, v in row.get("windowPerformances", []):
            if w == window:
                return float(v.get("pnl", 0)), float(v.get("roi", 0))
        return 0.0, 0.0

    whales = []
    for row in rows:
        av = float(row["accountValue"])
        if av < WHALE_MIN_USD:
            continue
        d_pnl, _ = perf(row, "day")
        w_pnl, _ = perf(row, "week")
        m_pnl, _ = perf(row, "month")
        whales.append(dict(address=row["ethAddress"], account_value=av,
                           day_pnl=d_pnl, week_pnl=w_pnl, month_pnl=m_pnl))
    whales.sort(key=lambda x: x["account_value"], reverse=True)
    return whales[:MAX_WHALES]


def get_prices():
    """Mark price per coin from metaAndAssetCtxs (one cheap call)."""
    res = _post({"type": "metaAndAssetCtxs"})
    if not res:
        return {}
    meta, ctxs = res[0], res[1]
    names = [u["name"] for u in meta["universe"]]
    out = {}
    for name, ctx in zip(names, ctxs):
        px = ctx.get("markPx") or ctx.get("midPx") or ctx.get("oraclePx")
        if px:
            out[name] = float(px)
    return out


def get_positions(address):
    st = _post({"type": "clearinghouseState", "user": address})
    if not st:
        return None, []
    acct_val = float(st.get("marginSummary", {}).get("accountValue", 0))
    out = []
    for ap in st.get("assetPositions", []):
        p = ap.get("position", {})
        szi = float(p.get("szi", 0))
        if szi == 0:
            continue
        out.append(dict(
            coin=p["coin"],
            szi=szi,
            side="long" if szi > 0 else "short",
            entry_px=float(p.get("entryPx") or 0),
            notional=float(p.get("positionValue") or 0),
            upnl=float(p.get("unrealizedPnl") or 0),
            roe=float(p.get("returnOnEquity") or 0),
            liq_px=float(p.get("liquidationPx") or 0) if p.get("liquidationPx") else 0.0,
            lev=float(p.get("leverage", {}).get("value") or 0),
        ))
    return acct_val, out


def init_db(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS whales (
        address TEXT PRIMARY KEY, account_value REAL,
        day_pnl REAL, week_pnl REAL, month_pnl REAL, updated_ts INTEGER);
    CREATE TABLE IF NOT EXISTS positions (
        ts INTEGER, address TEXT, coin TEXT, szi REAL, side TEXT,
        entry_px REAL, notional REAL, upnl REAL, roe REAL, liq_px REAL, lev REAL);
    CREATE INDEX IF NOT EXISTS ix_pos_ts ON positions(ts);
    CREATE INDEX IF NOT EXISTS ix_pos_coin_ts ON positions(coin, ts);
    CREATE TABLE IF NOT EXISTS sweeps (ts INTEGER PRIMARY KEY, whales INTEGER, positions INTEGER);
    CREATE TABLE IF NOT EXISTS prices (ts INTEGER, coin TEXT, mark_px REAL);
    CREATE INDEX IF NOT EXISTS ix_prices_ts ON prices(ts);
    """)
    con.commit()


def sweep(con):
    ts = int(time.time())
    whales = get_leaderboard()
    prices = get_prices()
    for coin, px in prices.items():
        con.execute("INSERT INTO prices VALUES (?,?,?)", (ts, coin, px))
    print(f"[{time.strftime('%H:%M:%S')}] leaderboard: {len(whales)} whales >= ${WHALE_MIN_USD:,.0f}, "
          f"{len(prices)} mark prices")
    n_pos = 0
    for i, w in enumerate(whales):
        con.execute("INSERT OR REPLACE INTO whales VALUES (?,?,?,?,?,?)",
                    (w["address"], w["account_value"], w["day_pnl"],
                     w["week_pnl"], w["month_pnl"], ts))
        _, positions = get_positions(w["address"])
        for p in positions:
            con.execute(
                "INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts, w["address"], p["coin"], p["szi"], p["side"], p["entry_px"],
                 p["notional"], p["upnl"], p["roe"], p["liq_px"], p["lev"]))
            n_pos += 1
        time.sleep(REQ_DELAY)
        if (i + 1) % 50 == 0:
            con.commit()
            print(f"  ...{i+1}/{len(whales)} whales, {n_pos} positions so far")
    con.execute("INSERT OR REPLACE INTO sweeps VALUES (?,?,?)", (ts, len(whales), n_pos))
    con.commit()
    print(f"[{time.strftime('%H:%M:%S')}] sweep done: {len(whales)} whales, {n_pos} open positions stored")


def main():
    con = sqlite3.connect(DB_PATH)
    init_db(con)
    once = os.environ.get("ONCE") == "1"
    while True:
        try:
            sweep(con)
        except Exception as e:
            print("sweep error:", e)
        if once:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
