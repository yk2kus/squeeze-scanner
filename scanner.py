import asyncio, aiohttp, json, os, sqlite3, time, math
from collections import defaultdict, deque
from datetime import datetime, timezone

BINANCE_REST = "https://fapi.binance.com"
BINANCE_WS = "wss://fstream.binance.com/stream"

with open("config.json") as f:
    CFG = json.load(f)

DB = CFG["db_path"]
state = defaultdict(lambda: {
    "prices": deque(maxlen=30),
    "oi": deque(maxlen=30),
    "vol": deque(maxlen=60),
    "buyvol": deque(maxlen=60),
    "liq_short": deque(maxlen=60),
    "liq_long": deque(maxlen=60),
    "funding": 0.0,
    "last_score": 0,
    "last_alert": 0
})

def db_init():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS observations(
        ts REAL, symbol TEXT, price REAL, oi REAL, funding REAL,
        volume REAL, buy_volume REAL, short_liq REAL, long_liq REAL,
        score INTEGER, reasons TEXT)""")
    con.commit(); con.close()

def db_write(row):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?)", row)
    con.commit(); con.close()

async def get_symbols(session):
    if CFG["symbols"]:
        return [s.lower() for s in CFG["symbols"]]
    async with session.get(BINANCE_REST + "/fapi/v1/exchangeInfo") as r:
        data = await r.json()
    out = []
    for x in data["symbols"]:
        if x["status"] == "TRADING" and x["contractType"] == "PERPETUAL" and x["quoteAsset"] == "USDT":
            out.append(x["symbol"].lower())
    return out

async def send_telegram(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat or not CFG.get("telegram_enabled", True):
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={"chat_id": chat, "text": text})
    except Exception as e:
        print("Telegram error:", e)

def pct(a,b):
    return (b/a-1)*100 if a and b else 0

def score_symbol(sym):
    s = state[sym]
    now = time.time()
    reasons=[]; score=0
    if len(s["prices"]) < 4: return 0, reasons
    p0=s["prices"][0][1]; p1=s["prices"][-1][1]
    pp=pct(p0,p1)

    if pp >= 1.5: score += 2; reasons.append(f"price +{pp:.2f}%")
    elif pp >= 0.8: score += 1; reasons.append(f"price +{pp:.2f}%")

    if len(s["oi"]) >= 4:
        o0=s["oi"][0][1]; o1=s["oi"][-1][1]
        op=pct(o0,o1)
        if pp > 0 and op <= -3: score += 2; reasons.append(f"OI {op:.2f}%")
        elif pp > 0 and op <= -1.5: score += 1; reasons.append(f"OI {op:.2f}%")

    # Recent short liquidation notional relative to its rolling baseline.
    recent=sum(x[1] for x in list(s["liq_short"])[-3:])
    baseline=sum(x[1] for x in s["liq_short"]) / max(1,len(s["liq_short"]))
    if recent > max(10000, baseline*5): score += 2; reasons.append("short liquidation burst")
    elif recent > max(10000, baseline*2): score += 1; reasons.append("short liquidations elevated")

    if s["funding"] <= -0.0005: score += 1; reasons.append(f"funding {s['funding']*100:.3f}%")

    recent_buy=sum(x[1] for x in list(s["buyvol"])[-3:])
    recent_vol=sum(x[1] for x in list(s["vol"])[-3:])
    if recent_vol and recent_buy/recent_vol >= .65:
        score += 1; reasons.append("aggressive buying")

    if len(s["vol"]) >= 10:
        recent_v=sum(x[1] for x in list(s["vol"])[-3:])
        base_v=sum(x[1] for x in list(s["vol"])[:-3]) / max(1,len(s["vol"])-3)
        if recent_v > base_v*3: score += 1; reasons.append("volume burst")

    return min(score,10), reasons

async def consume(symbols):
    streams=[]
    for s in symbols:
        streams += [
            f"{s}@kline_1m",
            f"{s}@forceOrder",
            f"{s}@markPrice@1s",
            f"{s}@aggTrade"
        ]
    # Binance combined streams can become large; split if necessary.
    for i in range(0,len(streams),150):
        asyncio.create_task(ws_group(streams[i:i+150]))

async def ws_group(streams):
    url=BINANCE_WS+"?streams="+"/".join(streams)
    while True:
        try:
            import websockets
            async with websockets.connect(url, ping_interval=20, max_size=None) as ws:
                async for raw in ws:
                    msg=json.loads(raw)
                    d=msg.get("data",{})
                    stream=msg.get("stream","")
                    sym=d.get("s","").lower()
                    if not sym: continue
                    now=time.time(); st=state[sym]

                    if "@kline_" in stream and d.get("e")=="kline":
                        k=d["k"]
                        if k["x"]:
                            st["prices"].append((now,float(k["c"])))
                            st["vol"].append((now,float(k["q"])))
                            # kline taker buy quote volume
                            st["buyvol"].append((now,float(k["Q"])))

                    elif d.get("e")=="forceOrder":
                        o=d.get("o",{})
                        qty=float(o.get("q",0)); price=float(o.get("ap") or o.get("p") or 0)
                        notional=qty*price
                        if o.get("S")=="SELL":
                            st["liq_short"].append((now,notional))
                        else:
                            st["liq_long"].append((now,notional))

                    elif d.get("e")=="markPriceUpdate":
                        # funding field is included in markPriceUpdate streams
                        if "r" in d:
                            st["funding"]=float(d["r"])
                        st["prices"].append((now,float(d["p"])))

                    elif d.get("e")=="aggTrade":
                        # Keep event stream alive; kline handles volume aggregation.
                        pass

                    # OI is sampled separately below.
        except Exception as e:
            print("WS reconnect:", e)
            await asyncio.sleep(2)

async def oi_loop(symbols):
    async with aiohttp.ClientSession() as session:
        while True:
            for i in range(0,len(symbols),20):
                batch=symbols[i:i+20]
                await asyncio.gather(*(sample_oi(session,s) for s in batch))
            await asyncio.sleep(20)

async def sample_oi(session,sym):
    try:
        async with session.get(BINANCE_REST+"/fapi/v1/openInterest",params={"symbol":sym.upper()}) as r:
            if r.status==200:
                d=await r.json()
                state[sym]["oi"].append((time.time(),float(d["openInterest"])))
    except Exception as e:
        print("OI:",sym,e)

async def scoring_loop():
    while True:
        now=time.time()
        for sym,st in list(state.items()):
            score,reasons=score_symbol(sym)
            if score:
                price=st["prices"][-1][1] if st["prices"] else 0
                oi=st["oi"][-1][1] if st["oi"] else 0
                row=(now,sym.upper(),price,oi,st["funding"],
                     sum(x[1] for x in list(st["vol"])[-3:]),
                     sum(x[1] for x in list(st["buyvol"])[-3:]),
                     sum(x[1] for x in list(st["liq_short"])[-3:]),
                     sum(x[1] for x in list(st["liq_long"])[-3:]),
                     score,"; ".join(reasons))
                db_write(row)
                if score >= CFG["score_alert_threshold"] and now-st["last_alert"] >= CFG["repeat_alert_minutes"]*60:
                    st["last_alert"]=now
                    msg=f"🚨 SQUEEZE {sym.upper()} | score {score}/10\n" + "\n".join("• "+x for x in reasons)
                    print(msg)
                    await send_telegram(msg)
        await asyncio.sleep(10)

async def main():
    db_init()
    async with aiohttp.ClientSession() as session:
        symbols=await get_symbols(session)
    print(f"Monitoring {len(symbols)} USDT perpetuals")
    await consume(symbols)
    await asyncio.gather(oi_loop(symbols), scoring_loop())

if __name__=="__main__":
    asyncio.run(main())
