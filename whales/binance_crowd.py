#!/usr/bin/env python3
"""
Binance CROWD layer (aggregate positioning) — the complement to the Hyperliquid
WHALE layer. Binance is a CEX: individual positions are private, so all we can
get is AGGREGATE sentiment. That's still the other half of the key signal:

    whales (Hyperliquid, per-position)  vs  crowd (Binance, aggregate)
    -> divergence = the 'smart money short / crowd long' read.

We only query coins we already track whale data for (from whales.db), so the
universe is small and the divergence is computable.

DISCIPLINE (why the old scanners banned the IP — never again):
  * ONE instance only, guarded by an flock lock.
  * Small universe (only coins in whales.db), SLOW cadence (default 30 min).
  * Read the X-MBX-USED-WEIGHT-1M header and throttle *before* the limit.
  * On 418 / -1003 (ban), parse 'banned until' and sleep past it — never hammer.
  * exchangeInfo symbol list cached to disk (SYMBOLS_CACHE), refreshed once/day.
These are the rules from the repo memory (binance-scanner-websocket-first).
"""
import fcntl
import json
import os
import re
import sqlite3
import time
import urllib.request
import urllib.error

FAPI = "https://fapi.binance.com"
HERE = os.path.dirname(__file__)
WHALE_DB = os.environ.get("WHALE_DB_PATH", os.path.join(HERE, "whales.db"))
CROWD_DB = os.environ.get("CROWD_DB_PATH", os.path.join(HERE, "crowd.db"))
SYMBOLS_CACHE = os.environ.get("SYMBOLS_CACHE", os.path.join(HERE, "binance_symbols.json"))
LOCK_PATH = os.environ.get("CROWD_LOCK", os.path.join(HERE, ".crowd.lock"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "1800"))   # 30 min — gentle
REQ_DELAY = float(os.environ.get("REQ_DELAY", "0.3"))       # gap between REST calls
WEIGHT_SOFT = int(os.environ.get("WEIGHT_SOFT", "1000"))    # back off well under 2400


def _get(path, params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{FAPI}{path}?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "crowd/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            used = int(r.headers.get("X-MBX-USED-WEIGHT-1M", "0") or 0)
            data = json.load(r)
            return data, used
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        if e.code in (418, 429) or '"code":-1003' in body:
            m = re.search(r"banned until (\d+)", body)
            if m:
                until = int(m.group(1)) / 1000
                wait = max(5, until - time.time() + 5)
                print(f"  BANNED until {time.strftime('%H:%M', time.localtime(until))} "
                      f"— sleeping {wait/60:.1f} min (discipline: never hammer)")
                time.sleep(wait)
            else:
                print("  rate-limited, backing off 60s")
                time.sleep(60)
        else:
            print(f"  HTTP {e.code}: {body[:80]}")
        return None, None


def load_binance_perps():
    """Cached set of Binance USDT-perp symbols (exchangeInfo once/day)."""
    if os.path.exists(SYMBOLS_CACHE) and time.time() - os.path.getmtime(SYMBOLS_CACHE) < 86400:
        return set(json.load(open(SYMBOLS_CACHE)))
    data, _ = _get("/fapi/v1/exchangeInfo", {})
    if not data:
        return set(json.load(open(SYMBOLS_CACHE))) if os.path.exists(SYMBOLS_CACHE) else set()
    syms = {s["symbol"] for s in data.get("symbols", [])
            if s.get("contractType") == "PERPETUAL" and s["symbol"].endswith("USDT")}
    json.dump(sorted(syms), open(SYMBOLS_CACHE, "w"))
    print(f"  cached {len(syms)} Binance USDT-perp symbols")
    return syms


def tracked_coins():
    """HL coins we have whale data for -> map to Binance COINUSDT symbols."""
    con = sqlite3.connect(WHALE_DB)
    ts = con.execute("SELECT MAX(ts) FROM positions").fetchone()[0]
    coins = [r[0] for r in con.execute(
        "SELECT DISTINCT coin FROM positions WHERE ts=?", (ts,)).fetchall()]
    con.close()
    return coins


def init_db(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS crowd (
        ts INTEGER, coin TEXT, symbol TEXT,
        global_long REAL, top_acct_long REAL, top_pos_long REAL);
    CREATE INDEX IF NOT EXISTS ix_crowd_ts ON crowd(ts);
    """)
    con.commit()


def _ratio(path, symbol):
    data, used = _get(path, {"symbol": symbol, "period": "1h", "limit": 1})
    time.sleep(REQ_DELAY)
    if used and used > WEIGHT_SOFT:
        print(f"  weight {used} > {WEIGHT_SOFT}, cooling 20s")
        time.sleep(20)
    if data and isinstance(data, list) and data:
        return float(data[0].get("longAccount", data[0].get("longShortRatio", 0)))
    return None


def sweep(con):
    ts = int(time.time())
    perps = load_binance_perps()
    coins = tracked_coins()
    done = 0
    for coin in coins:
        sym = coin + "USDT"
        if sym not in perps:
            continue   # coin not on Binance (HL-only) — skip
        g = _ratio("/futures/data/globalLongShortAccountRatio", sym)
        ta = _ratio("/futures/data/topLongShortAccountRatio", sym)
        tp = _ratio("/futures/data/topLongShortPositionRatio", sym)
        if g is None and ta is None and tp is None:
            continue
        con.execute("INSERT INTO crowd VALUES (?,?,?,?,?,?)",
                    (ts, coin, sym, g, ta, tp))
        done += 1
        if done % 20 == 0:
            con.commit()
            print(f"  ...{done} coins")
    con.commit()
    print(f"[{time.strftime('%H:%M:%S')}] crowd sweep: {done} coins stored")


def main():
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another crowd collector is already running — exiting (one instance only)")
        return
    con = sqlite3.connect(CROWD_DB)
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
