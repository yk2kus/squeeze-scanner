"""
Squeeze scanner v2 — state machine + adaptive baselines + evaluation DB.

Phases:  NORMAL → STARTING → ACTIVE → EXHAUSTED → NORMAL   (STARTING → FAILED)
All thresholds compare each coin to ITS OWN rolling baseline (adaptive), so a
liquidation "5x normal" means the same thing for BTC and a thin altcoin.

Alert/research only — never places orders. The point is the events table:
for every STARTING we record forward returns, so later we can ask
"when it says STARTING, what actually happens?" instead of assuming.
"""
import asyncio, aiohttp, json, os, sqlite3, time
from collections import defaultdict, deque
from datetime import datetime, timezone

BINANCE_REST="https://fapi.binance.com"
BINANCE_WS="wss://fstream.binance.com/stream"
with open("config.json") as f: CFG=json.load(f)
DB=os.getenv("DB_PATH") or CFG["db_path"]

def blank():
    return {"prices":deque(maxlen=40),"oi":deque(maxlen=40),"vol":deque(maxlen=80),
            "buyvol":deque(maxlen=80),"liq_short":deque(maxlen=80),"liq_long":deque(maxlen=80),
            "funding":0.0,"phase":"NORMAL","baseline_price":None,"baseline_oi":None,
            "start_time":None,"peak_price":None,"peak_liq":0.0,"start_score":0,
            "exhaust_score":0,"phase_time":0.0,"alerted":None}
state=defaultdict(blank)
open_events={}   # event_id -> tracking dict for forward returns
SAMPLES=[1,3,5,15,30,60]  # minutes

# ---------------------------------------------------------------- db
def db_init():
    c=sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS events(
      id TEXT PRIMARY KEY, symbol TEXT, start_time REAL, start_price REAL,
      start_oi REAL, start_score INT, reached_active INT DEFAULT 0,
      final_phase TEXT, p1m REAL,p3m REAL,p5m REAL,p15m REAL,p30m REAL,p60m REAL,
      mfe REAL, mae REAL, time_to_peak REAL, closed INT DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS live(
      symbol TEXT PRIMARY KEY, phase TEXT, start_score INT, exhaust_score INT,
      price_from_start REAL, oi_from_start REAL, liq_mult REAL, vol_mult REAL,
      taker_pct REAL, funding REAL, cur_price REAL, updated REAL)""")
    c.commit(); c.close()

def db(q,args=()):
    c=sqlite3.connect(DB); c.execute(q,args); c.commit(); c.close()

# ---------------------------------------------------------------- metrics
def pct(a,b): return (b/a-1)*100 if a and b else 0.0
def liq_mult(s):
    base=sum(x[1] for x in s["liq_short"])/max(len(s["liq_short"]),1)
    recent=sum(x[1] for x in list(s["liq_short"])[-3:])
    return recent/max(base,1.0), recent
def vol_mult(s):
    if len(s["vol"])<6: return 1.0
    recent=sum(x[1] for x in list(s["vol"])[-3:])/3
    base=sum(x[1] for x in list(s["vol"])[:-3])/max(len(s["vol"])-3,1)
    return recent/max(base,1e-9)
def taker_pct(s):
    rb=sum(x[1] for x in list(s["buyvol"])[-3:]); rv=sum(x[1] for x in list(s["vol"])[-3:])
    return (rb/rv*100) if rv else 0.0

def start_score(s):
    r=[]; sc=0
    if len(s["prices"])<4: return 0,r,{}
    pp=pct(s["prices"][0][1],s["prices"][-1][1])
    if pp>=1.5: sc+=2; r.append(f"price +{pp:.2f}%")
    elif pp>=0.8: sc+=1; r.append(f"price +{pp:.2f}%")
    op=0.0
    if len(s["oi"])>=4:
        op=pct(s["oi"][0][1],s["oi"][-1][1])
        if pp>0 and op<=-3: sc+=2; r.append(f"OI {op:.2f}%")
        elif pp>0 and op<=-1.5: sc+=1; r.append(f"OI {op:.2f}%")
    lm,_=liq_mult(s)
    if lm>=5: sc+=2; r.append(f"short liq {lm:.1f}x")
    elif lm>=2: sc+=1; r.append(f"short liq {lm:.1f}x")
    if s["funding"]<=-0.0002: sc+=1; r.append(f"funding {s['funding']*100:.3f}%")
    if taker_pct(s)>=60: sc+=1; r.append(f"taker {taker_pct(s):.0f}%")
    if vol_mult(s)>=2: sc+=1; r.append(f"vol {vol_mult(s):.1f}x")
    return min(sc,10),r,{"pp":pp,"op":op,"lm":lm}

def exhaust_score(s,price):
    sc=0; r=[]
    if s["baseline_oi"]:
        oic=pct(s["baseline_oi"],s["oi"][-1][1]) if s["oi"] else 0
        if oic<=-8: sc+=2; r.append(f"OI collapsed {oic:.1f}%")
        elif oic<=-5: sc+=2; r.append(f"OI -{abs(oic):.1f}%")
    if s["baseline_price"]:
        g=pct(s["baseline_price"],price)
        if g>=8: sc+=2; r.append(f"gain +{g:.1f}%")
        elif g>=5: sc+=1; r.append(f"gain +{g:.1f}%")
    lm,_=liq_mult(s)
    if lm>=10: sc+=2; r.append(f"liq extreme {lm:.1f}x")
    elif lm>=5: sc+=1; r.append(f"liq major {lm:.1f}x")
    if vol_mult(s)>=5: sc+=1; r.append(f"vol {vol_mult(s):.1f}x")
    # peak fading: current price below peak and taker fading
    if s["peak_price"] and price < s["peak_price"]*0.995 and taker_pct(s)<55:
        sc+=1; r.append("momentum fading")
    return min(sc,10),r

# ------------------------------------------------------ state machine
async def classify(sym):
    s=state[sym]; now=time.time()
    if len(s["prices"])<4 or len(s["oi"])<3: return
    price=s["prices"][-1][1]; oi=s["oi"][-1][1] if s["oi"] else 0
    ss,sr,m=start_score(s); s["start_score"]=ss
    lm,_=liq_mult(s); vm=vol_mult(s); tk=taker_pct(s)

    # NORMAL -> STARTING
    if s["phase"]=="NORMAL":
        if ss>=CFG["score_alert_threshold"]:
            s.update(phase="STARTING",baseline_price=price,baseline_oi=oi,
                     start_time=now,peak_price=price,phase_time=now)
            eid=f"{sym}-{int(now)}"
            open_events[eid]={"sym":sym,"t0":now,"p0":price,"oi0":oi,"score":ss,
                              "samples":{k:None for k in SAMPLES},"mfe":0.0,"mae":0.0,
                              "peak_t":now,"active":0}
            db("""INSERT OR REPLACE INTO events(id,symbol,start_time,start_price,start_oi,
                  start_score,final_phase) VALUES(?,?,?,?,?,?,?)""",
               (eid,sym,now,price,oi,ss,"STARTING"))
            await alert(sym,"🟢 SQUEEZE STARTING",s,ss,0,sr,lm,vm,tk)

    elif s["phase"]=="STARTING":
        s["peak_price"]=max(s["peak_price"],price)
        confirm = m["pp"]>0 and m["op"]<=-1.5 and lm>=2 and vm>=1.5
        died = (price<=s["baseline_price"]*1.003 and m["op"]>-1.0 and lm<1.5)
        if confirm:
            s["phase"]="ACTIVE"; s["phase_time"]=now
            for e in open_events.values():
                if e["sym"]==sym and e["active"]==0: e["active"]=1
            db("UPDATE events SET reached_active=1,final_phase='ACTIVE' WHERE symbol=? AND closed=0",(sym,))
            ex,er=exhaust_score(s,price)
            await alert(sym,"🟠 SQUEEZE ACTIVE",s,ss,ex,sr,lm,vm,tk)
        elif died or (now-s["phase_time"]>600):
            s["phase"]="NORMAL"
            db("UPDATE events SET final_phase='FAILED' WHERE symbol=? AND closed=0",(sym,))
            await alert(sym,"⚪ SQUEEZE FAILED",s,ss,0,["momentum died"],lm,vm,tk)
            _reset(s)

    elif s["phase"]=="ACTIVE":
        s["peak_price"]=max(s["peak_price"],price)
        ex,er=exhaust_score(s,price)
        s["exhaust_score"]=ex
        if ex>=CFG["score_alert_threshold"]:
            s["phase"]="EXHAUSTED"; s["phase_time"]=now
            db("UPDATE events SET final_phase='EXHAUSTED' WHERE symbol=? AND closed=0",(sym,))
            await alert(sym,"🔴 SQUEEZE EXHAUSTED",s,ss,ex,er,lm,vm,tk)
        elif ss<2 and lm<1.5 and now-s["phase_time"]>300:
            s["phase"]="NORMAL"; _reset(s)

    elif s["phase"]=="EXHAUSTED":
        if lm<1.5 and ss<3 and now-s["phase_time"]>300:
            s["phase"]="NORMAL"; _reset(s)

    # persist live phase (drop from board when NORMAL)
    if s["phase"]=="NORMAL":
        db("DELETE FROM live WHERE symbol=?",(sym,))
    else:
        ex=s.get("exhaust_score",0)
        db("""INSERT OR REPLACE INTO live VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
           (sym,s["phase"],ss,ex,pct(s["baseline_price"],price),
            pct(s["baseline_oi"],oi),lm,vm,tk,s["funding"],price,now))

def _reset(s):
    s.update(baseline_price=None,baseline_oi=None,start_time=None,peak_price=None,
             peak_liq=0.0,exhaust_score=0,alerted=None)

# ------------------------------------------------- forward-return eval
def track_events():
    now=time.time(); done=[]
    for eid,e in open_events.items():
        s=state[e["sym"]]; price=s["prices"][-1][1] if s["prices"] else e["p0"]
        chg=pct(e["p0"],price)
        if chg>e["mfe"]: e["mfe"]=chg; e["peak_t"]=now
        if chg<e["mae"]: e["mae"]=chg
        mins=(now-e["t0"])/60
        for k in SAMPLES:
            if e["samples"][k] is None and mins>=k:
                e["samples"][k]=chg
        if mins>=60:
            sm=e["samples"]
            db("""UPDATE events SET p1m=?,p3m=?,p5m=?,p15m=?,p30m=?,p60m=?,
                  mfe=?,mae=?,time_to_peak=?,closed=1 WHERE id=?""",
               (sm[1],sm[3],sm[5],sm[15],sm[30],sm[60],e["mfe"],e["mae"],
                (e["peak_t"]-e["t0"])/60,eid))
            done.append(eid)
    for eid in done: open_events.pop(eid,None)

# ---------------------------------------------------------- telegram
async def alert(sym,head,s,ss,ex,reasons,lm,vm,tk):
    print(f"{head} {sym} start {ss}/10 exh {ex}/10 | "+", ".join(reasons))
    token,chat=os.getenv("TELEGRAM_BOT_TOKEN"),os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat or not CFG.get("telegram_enabled",True): return
    price=s["prices"][-1][1] if s["prices"] else 0
    body=(f"{head}\n\n{sym}\nStart score: {ss}/10"+(f"\nExhaust score: {ex}/10" if ex else "")+
          f"\n\nPrice from start: {pct(s['baseline_price'],price):+.2f}%" if s["baseline_price"] else "")
    body+=(f"\nOI from start: {pct(s['baseline_oi'],s['oi'][-1][1]):+.2f}%" if s["baseline_oi"] and s["oi"] else "")
    body+=f"\nShort liq: {lm:.1f}x baseline\nVolume: {vm:.1f}x\nTaker buy: {tk:.0f}%\nFunding: {s['funding']*100:.3f}%\nPhase: {s['phase']}"
    try:
        async with aiohttp.ClientSession() as se:
            await se.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id":chat,"text":body})
    except Exception as e: print("tg:",e)

# --------------------------------------------------------- ws / loops
async def get_symbols(session):
    if CFG["symbols"]: return [x.lower() for x in CFG["symbols"]]
    # exchangeInfo can be rate-limited (returns {"code":...} with no "symbols").
    # Retry with backoff instead of crashing.
    for attempt in range(35):
        try:
            async with session.get(BINANCE_REST+"/fapi/v1/exchangeInfo") as r:
                d=await r.json()
            if isinstance(d,dict) and "symbols" in d:
                return [x["symbol"].lower() for x in d["symbols"]
                        if x["status"]=="TRADING" and x["contractType"]=="PERPETUAL"
                        and x["quoteAsset"]=="USDT"]
            print(f"exchangeInfo not ready (attempt {attempt+1}): {str(d)[:80]}")
        except Exception as e:
            print(f"exchangeInfo error (attempt {attempt+1}): {e}")
        await asyncio.sleep(60)
    raise RuntimeError("could not fetch exchangeInfo after retries")

async def ws_group(streams):
    import websockets
    url=BINANCE_WS+"?streams="+"/".join(streams)
    while True:
        try:
            async with websockets.connect(url,ping_interval=20,max_size=None) as ws:
                async for raw in ws:
                    msg=json.loads(raw); d=msg.get("data",{}); stream=msg.get("stream","")
                    sym=d.get("s","").lower()
                    if not sym: continue
                    now=time.time(); st=state[sym]
                    if "@kline_" in stream and d.get("e")=="kline" and d["k"]["x"]:
                        k=d["k"]; st["prices"].append((now,float(k["c"])))
                        st["vol"].append((now,float(k["q"]))); st["buyvol"].append((now,float(k["Q"])))
                    elif d.get("e")=="forceOrder":
                        o=d.get("o",{}); notion=float(o.get("q",0))*float(o.get("ap") or o.get("p") or 0)
                        (st["liq_short"] if o.get("S")=="SELL" else st["liq_long"]).append((now,notion))
                    elif d.get("e")=="markPriceUpdate":
                        if "r" in d: st["funding"]=float(d["r"])
                        st["prices"].append((now,float(d["p"])))
        except Exception as e:
            print("WS reconnect:",e); await asyncio.sleep(2)

async def oi_loop(symbols):
    async with aiohttp.ClientSession() as se:
        while True:
            for i in range(0,len(symbols),20):
                await asyncio.gather(*(sample_oi(se,s) for s in symbols[i:i+20]))
            await asyncio.sleep(30)
async def sample_oi(se,sym):
    try:
        async with se.get(BINANCE_REST+"/fapi/v1/openInterest",params={"symbol":sym.upper()}) as r:
            if r.status==200:
                d=await r.json(); state[sym]["oi"].append((time.time(),float(d["openInterest"])))
    except Exception: pass

async def scoring_loop():
    while True:
        for sym in list(state.keys()):
            try: await classify(sym)
            except Exception as e: print("classify",sym,e)
        track_events()
        await asyncio.sleep(10)

async def main():
    db_init()
    async with aiohttp.ClientSession() as se: symbols=await get_symbols(se)
    print(f"Monitoring {len(symbols)} USDT perpetuals (v2 state machine)")
    streams=[]
    for s in symbols: streams+= [f"{s}@kline_1m",f"{s}@forceOrder",f"{s}@markPrice@1s"]
    for i in range(0,len(streams),150): asyncio.create_task(ws_group(streams[i:i+150]))
    await asyncio.gather(oi_loop(symbols),scoring_loop())

if __name__=="__main__": asyncio.run(main())
