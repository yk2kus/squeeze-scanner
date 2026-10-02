#!/usr/bin/env python3
"""
Render whales.db -> reports/whales.html  (modern pumpglass-style dashboard).

Layout: summary cards + pill tabs, all driven by a persistent coin selector.
  • Positioning   — per-coin whale long/short (stacked bar) + Binance crowd + divergence
  • Position book — every whale in the selected coin (clickable/copyable address)
  • Liq heatmap   — actual on-chain liquidation prices × size

Data sources: Hyperliquid (per-position whales, public on-chain) + Binance
(aggregate crowd, when the collector has run). All embedded as JSON so the UI is
instant & client-side.
"""
import json
import os
import sqlite3
import time
import urllib.request

DB_PATH = os.environ.get("WHALE_DB_PATH", os.path.join(os.path.dirname(__file__), "whales.db"))
CROWD_DB = os.environ.get("CROWD_DB_PATH", os.path.join(os.path.dirname(__file__), "crowd.db"))
REPORTS_DIR = os.environ.get("WHALE_REPORTS_DIR", os.path.join(os.path.dirname(__file__), "reports"))
OUT = os.path.join(REPORTS_DIR, "whales.html")
MIN_COIN_NOTIONAL = float(os.environ.get("MIN_COIN_NOTIONAL", "1000000"))
EXPLORER = os.environ.get("WHALE_EXPLORER", "https://app.hyperliquid.xyz/explorer/address/")


def latest_ts(con):
    return con.execute("SELECT MAX(ts) FROM positions").fetchone()[0]


def per_coin(con, ts):
    rows = con.execute("""
        SELECT coin,
               SUM(CASE WHEN side='long'  THEN 1 ELSE 0 END),
               SUM(CASE WHEN side='short' THEN 1 ELSE 0 END),
               SUM(CASE WHEN side='long'  THEN notional ELSE 0 END),
               SUM(CASE WHEN side='short' THEN notional ELSE 0 END),
               SUM(upnl),
               SUM(notional * entry_px) / NULLIF(SUM(notional),0)
        FROM positions WHERE ts=? GROUP BY coin
    """, (ts,)).fetchall()
    out = []
    for coin, nl, ns, ln, sn, upnl, wentry in rows:
        tot = (ln or 0) + (sn or 0)
        if tot < MIN_COIN_NOTIONAL:
            continue
        out.append(dict(coin=coin, n_long=nl, n_short=ns, long_ntl=ln or 0,
                        short_ntl=sn or 0, net=(ln or 0) - (sn or 0),
                        long_pct=100.0 * (ln or 0) / tot if tot else 0,
                        upnl=upnl or 0, wentry=wentry or 0, tot=tot))
    out.sort(key=lambda x: x["tot"], reverse=True)
    return out


def all_positions(con, ts):
    rows = con.execute("""
        SELECT address, coin, side, notional, entry_px, upnl, roe, lev, liq_px
        FROM positions WHERE ts=? ORDER BY notional DESC
    """, (ts,)).fetchall()
    return [dict(addr=a, coin=c, side=s, ntl=n, entry=e, upnl=u, roe=r, lev=l, liq=lq)
            for a, c, s, n, e, u, r, l, lq in rows]


def get_crowd(coins):
    if not os.path.exists(CROWD_DB):
        return {}
    try:
        c = sqlite3.connect(CROWD_DB)
        ts = c.execute("SELECT MAX(ts) FROM crowd").fetchone()[0]
        if not ts:
            return {}
        out = {}
        for coin, g, ta, tp in c.execute(
                "SELECT coin, global_long, top_acct_long, top_pos_long FROM crowd WHERE ts=?", (ts,)):
            out[coin] = (tp or ta or g)
        c.close()
        return out
    except sqlite3.OperationalError:
        return {}


def get_prices(con, ts, coins):
    want = {c["coin"] for c in coins}
    try:
        rows = con.execute("SELECT coin, mark_px FROM prices WHERE ts=?", (ts,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if rows:
        return {c: p for c, p in rows if c in want}
    try:
        req = urllib.request.Request("https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "allMids"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            mids = json.load(r)
        return {c: float(p) for c, p in mids.items() if c in want}
    except Exception as e:
        print("live price fallback failed:", e)
        return {}


CSS = """
:root{
  --bg:#0b0e14; --panel:#141a23; --panel2:#1a212c; --border:#242c39;
  --text:#e6edf3; --muted:#8792a3; --green:#26d07c; --red:#ff5c6c;
  --accent:#4c8dff; --amber:#f5b544;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;font-size:14px}
.num{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
.wrap{max-width:1240px;margin:0 auto;padding:28px 22px 60px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.long{color:var(--green)}.short{color:var(--red)}.muted{color:var(--muted)}
/* header */
.head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}
h1{font-size:22px;margin:0;font-weight:700;letter-spacing:-.02em}
.sub{color:var(--muted);font-size:13px;margin-top:4px}
.selwrap{position:relative}
#search{background:var(--panel);border:1px solid var(--border);color:var(--text);
  padding:10px 14px;border-radius:10px;font:inherit;width:220px;outline:none}
#search:focus{border-color:var(--accent)}
.chip{display:inline-flex;align-items:center;gap:8px;background:var(--panel2);
  border:1px solid var(--border);border-radius:999px;padding:5px 12px;font-size:13px;margin-left:10px}
.chip b{font-weight:700}.chip .x{cursor:pointer;color:var(--muted)}.chip .x:hover{color:var(--text)}
/* cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:20px 0 8px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:14px 16px}
.card .k{color:var(--muted);font-size:12px;font-weight:500}
.card .v{font-size:21px;font-weight:700;margin-top:6px;letter-spacing:-.01em}
.card .sm{font-size:13px;color:var(--muted);margin-top:2px}
/* tabs */
.tabs{display:inline-flex;background:var(--panel);border:1px solid var(--border);
  border-radius:12px;padding:4px;gap:2px;margin:22px 0 14px}
.tab{padding:9px 18px;border-radius:9px;cursor:pointer;color:var(--muted);font-size:13px;font-weight:500;transition:.12s}
.tab:hover{color:var(--text)}
.tab.active{background:var(--accent);color:#fff;font-weight:600}
.pane{display:none}.pane.active{display:block;animation:fade .18s ease}
@keyframes fade{from{opacity:0;transform:translateY(3px)}to{opacity:1}}
/* table */
.tblwrap{background:var(--panel);border:1px solid var(--border);border-radius:14px;overflow:hidden}
table{border-collapse:collapse;width:100%}
th,td{padding:11px 14px;text-align:right;white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em;
  background:var(--panel2);position:sticky;top:0;border-bottom:1px solid var(--border)}
td{border-bottom:1px solid var(--border);font-size:13px}
tbody tr:last-child td{border-bottom:none}
td:first-child,th:first-child{text-align:left}
tr.coin{cursor:pointer;transition:.1s}tr.coin:hover{background:var(--panel2)}
tr.coin.sel{background:#4c8dff1a;box-shadow:inset 3px 0 0 var(--accent)}
.coinname{font-weight:700}
/* stacked long/short bar */
.lsbar{display:inline-flex;height:8px;width:110px;border-radius:5px;overflow:hidden;background:var(--border);vertical-align:middle}
.lsbar .l{background:var(--green)}.lsbar .s{background:var(--red)}
.lspct{font-size:11px;color:var(--muted);margin-left:8px;vertical-align:middle}
/* badges */
.badge{display:inline-block;padding:3px 9px;border-radius:999px;font-size:11px;font-weight:700;letter-spacing:.02em}
.badge.long{background:#26d07c22;color:var(--green)}
.badge.short{background:#ff5c6c22;color:var(--red)}
.badge.mut{background:#8792a322;color:var(--muted)}
.cnt{display:inline-block;min-width:22px;text-align:center;padding:2px 6px;border-radius:6px;font-size:11px;font-weight:600}
.cnt.l{background:#26d07c1a;color:var(--green)}.cnt.s{background:#ff5c6c1a;color:var(--red)}
/* address */
a.addr{color:var(--accent)}
.copy{cursor:pointer;color:var(--muted);margin-left:8px;user-select:none;font-size:12px}
.copy:hover{color:var(--text)}.copy.ok{color:var(--green)}
/* book header */
.bookhdr{padding:14px 16px;border-bottom:1px solid var(--border);background:var(--panel2);font-size:13px}
/* heatmap */
.heatcard{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px 20px}
.hmrow{display:flex;align-items:center;height:17px;font-size:11px;line-height:17px}
.hmpx{width:120px;text-align:right;padding-right:10px;color:var(--muted);white-space:nowrap}
.hmtrack{flex:1;position:relative}
.hmbar{height:11px;border-radius:3px}.hmL{background:var(--green)}.hmS{background:var(--red)}
.hmval{padding-left:10px;color:var(--muted);white-space:nowrap;font-size:11px}
.hmnow .hmpx{color:var(--amber);font-weight:700}
.nowline{display:flex;align-items:center;margin:3px 0;color:var(--amber);font-weight:700;font-size:12px}
.nowline .hmpx{color:var(--amber)}
.hint{color:var(--muted);padding:8px 2px}
.legend{color:var(--muted);font-size:12px;margin-top:16px;line-height:1.6}
.disc{color:var(--muted);font-size:12px;margin-top:22px;line-height:1.6;border-top:1px solid var(--border);padding-top:14px}
"""


def render():
    con = sqlite3.connect(DB_PATH)
    ts = latest_ts(con)
    if not ts:
        print("no data yet")
        return
    age = int(time.time()) - ts
    coins = per_coin(con, ts)
    positions = all_positions(con, ts)
    prices = get_prices(con, ts, coins)
    crowd = get_crowd(coins)
    for c in coins:
        cl = crowd.get(c["coin"])
        c["crowd_long"] = round(cl * 100, 1) if cl is not None else None
    nwhales = con.execute("SELECT COUNT(DISTINCT address) FROM positions WHERE ts=?", (ts,)).fetchone()[0]

    # summary stats for the cards
    total_ntl = sum(c["tot"] for c in coins)
    total_upnl = sum(c["upnl"] for c in coins)
    net_long = max(coins, key=lambda c: c["net"]) if coins else None
    net_short = min(coins, key=lambda c: c["net"]) if coins else None

    def usd(x):
        a = abs(x); s = "-" if x < 0 else ""
        for u, d in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
            if a >= d:
                return f"{s}${a/d:.1f}{u}"
        return f"{s}${a:.0f}"

    data = dict(coins=coins, positions=positions, prices=prices, has_crowd=bool(crowd))

    upnl_cls = "long" if total_upnl >= 0 else "short"
    cards = f"""
    <div class=card><div class=k>Whales tracked</div><div class="v num">{nwhales}</div>
      <div class=sm>{len(coins)} coins</div></div>
    <div class=card><div class=k>Total whale notional</div><div class="v num">{usd(total_ntl)}</div>
      <div class=sm>open interest, tracked</div></div>
    <div class=card><div class=k>Aggregate whale uPnL</div><div class="v num {upnl_cls}">{usd(total_upnl)}</div>
      <div class=sm>are they winning?</div></div>
    <div class=card><div class=k>Most net-long coin</div><div class="v"><span class=long>{net_long['coin'] if net_long else '—'}</span></div>
      <div class="sm num">{usd(net_long['net']) if net_long else ''}</div></div>
    <div class=card><div class=k>Most net-short coin</div><div class="v"><span class=short>{net_short['coin'] if net_short else '—'}</span></div>
      <div class="sm num">{usd(net_short['net']) if net_short else ''}</div></div>
    """

    html = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content=120><title>Whale Positioning · Hyperliquid</title>
<style>{CSS}</style></head><body><div class=wrap>

<div class=head>
  <div>
    <h1>🐋 Whale Positioning</h1>
    <div class=sub>Hyperliquid · public on-chain · sweep {age//60}m {age%60}s ago · positioning is context, not a prediction</div>
  </div>
  <div class=selwrap>
    <input id=search placeholder="Search coin — e.g. BTC" autocomplete=off>
    <span id=chip></span>
  </div>
</div>

<div class=cards>{cards}</div>

<div class=tabs>
  <div class=tab data-t=pos>Positioning</div>
  <div class=tab data-t=detail>Coin detail</div>
</div>

<div class=pane id=pane-pos>
  <div class=tblwrap style="overflow-x:auto">
  <table><thead><tr>
    <th>Coin</th><th>Long / Short</th><th>Whales</th>
    <th>Long $</th><th>Short $</th><th>Net</th><th>Avg entry</th><th>Whale uPnL</th>
    <th>Crowd L%</th><th>Whale vs Crowd</th></tr></thead>
  <tbody id=coinBody></tbody></table></div>
</div>

<div class=pane id=pane-detail>
  <div id=detailPrompt class=hint></div>
  <div id=detailBody>
    <div class=tblwrap>
      <div id=bookHdr class=bookhdr></div>
      <div style="overflow-x:auto">
      <table><thead><tr><th>Address</th><th>Coin</th><th>Side</th><th>Notional</th>
      <th>Entry</th><th>uPnL</th><th>ROE</th><th>Lev</th><th>Liq price</th></tr></thead>
      <tbody id=posBody></tbody></table></div>
    </div>
    <div class=heatcard style="margin-top:16px">
      <h3 style="margin:0 0 8px;font-size:15px">Liquidation heatmap</h3>
      <div id=heatHint class=hint></div>
      <div id=heatWrap></div>
      <div class=legend>🟩 below price = <b class=long>long liquidations</b> (down-cascade fuel) &nbsp;·&nbsp;
        🟥 above price = <b class=short>short liquidations</b> (squeeze fuel)</div>
    </div>
  </div>
</div>

<div class=disc>Auto-refreshes every 120 s. Whale data = actual on-chain positions on Hyperliquid (per position).
Crowd data = Binance aggregate long/short (when collected). A high reading is <b>context, not a forecast</b> —
whales are frequently wrong (see aggregate uPnL). Not financial advice.</div>

<script>
const D = {json.dumps(data)};
const EXPLORER = {json.dumps(EXPLORER)};
let sel = "", tab = "pos";
const usd = x => {{const a=Math.abs(x),s=x<0?"-":"";
  if(a>=1e9)return s+"$"+(a/1e9).toFixed(1)+"B";
  if(a>=1e6)return s+"$"+(a/1e6).toFixed(1)+"M";
  if(a>=1e3)return s+"$"+(a/1e3).toFixed(1)+"K";return s+"$"+a.toFixed(0);}};
const g4 = x => {{
  x=Number(x); if(!isFinite(x))return"—"; const a=Math.abs(x);
  if(a>=1000)return x.toLocaleString('en-US',{{maximumFractionDigits:0}});
  if(a>=1)return x.toLocaleString('en-US',{{maximumFractionDigits:2}});
  if(a===0)return"0";
  return x.toPrecision(4).replace(/\\.?0+$/,'');
}};
const lsbar = lp => `<span class=lsbar><span class=l style="width:${{lp}}%"></span>`+
  `<span class=s style="width:${{100-lp}}%"></span></span><span class=lspct>${{lp.toFixed(0)}}% long</span>`;

function crowdCells(c){{
  if(c.crowd_long==null) return `<td class=muted>—</td><td><span class="badge mut">pending</span></td>`;
  const div=c.long_pct-c.crowd_long;
  let label,cls;
  if(div<-15){{label="whales short · crowd long";cls="short";}}
  else if(div>15){{label="whales long · crowd short";cls="long";}}
  else{{label="aligned";cls="mut";}}
  return `<td class=num>${{c.crowd_long.toFixed(0)}}%</td>`+
    `<td><span class="badge ${{cls}}">${{div>0?'+':''}}${{div.toFixed(0)}} · ${{label}}</span></td>`;
}}

function coinRows(){{
  const b=document.getElementById('coinBody');b.innerHTML="";
  D.coins.forEach(c=>{{
    const sc=c.net>0?"long":"short", uc=c.upnl>=0?"long":"short";
    const tr=document.createElement('tr');tr.className="coin"+(c.coin===sel?" sel":"");
    tr.onclick=()=>openCoin(c.coin===sel?"":c.coin);
    tr.innerHTML=`<td class=coinname>${{c.coin}}</td>
      <td>${{lsbar(c.long_pct)}}</td>
      <td><span class="cnt l">${{c.n_long}}</span> <span class="cnt s">${{c.n_short}}</span></td>
      <td class="num long">${{usd(c.long_ntl)}}</td><td class="num short">${{usd(c.short_ntl)}}</td>
      <td class="num ${{sc}}">${{usd(c.net)}}</td><td class=num>${{g4(c.wentry)}}</td>
      <td class="num ${{uc}}">${{usd(c.upnl)}}</td>
      ${{crowdCells(c)}}`;
    b.appendChild(tr);
  }});
}}

function bookRows(){{
  const b=document.getElementById('posBody'), hdr=document.getElementById('bookHdr');b.innerHTML="";
  let rows=D.positions;
  if(sel){{
    rows=rows.filter(p=>p.coin===sel);
    const c=D.coins.find(x=>x.coin===sel);
    hdr.innerHTML=c?`<b>${{sel}}</b> &nbsp; <span class=long>${{c.n_long}} long</span> / `+
      `<span class=short>${{c.n_short}} short</span> &nbsp;·&nbsp; net <span class="num ${{c.net>=0?'long':'short'}}">${{usd(c.net)}}</span> `+
      `&nbsp;·&nbsp; whale uPnL <span class="num ${{c.upnl>=0?'long':'short'}}">${{usd(c.upnl)}}</span> `+
      `&nbsp;·&nbsp; avg entry <span class=num>${{g4(c.wentry)}}</span>`:"";
  }} else {{ rows=rows.slice(0,25); hdr.innerHTML="<span class=muted>Biggest positions overall — select a coin to focus.</span>"; }}
  rows.forEach(p=>{{
    const uc=p.upnl>=0?"long":"short";
    const tr=document.createElement('tr');
    tr.innerHTML=`<td><a class=addr href="${{EXPLORER}}${{p.addr}}" target=_blank rel=noopener title="${{p.addr}} — open in Hyperliquid explorer">${{p.addr.slice(0,6)}}…${{p.addr.slice(-4)}}</a><span class=copy title="copy address" onclick="copyAddr('${{p.addr}}',this)">⧉</span></td>
      <td class=coinname>${{p.coin}}</td>
      <td><span class="badge ${{p.side}}">${{p.side.toUpperCase()}}</span></td>
      <td class=num>${{usd(p.ntl)}}</td>
      <td class=num>${{g4(p.entry)}}</td><td class="num ${{uc}}">${{usd(p.upnl)}}</td>
      <td class="num ${{uc}}">${{(p.roe*100).toFixed(0)}}%</td><td class=num>${{p.lev.toFixed(0)}}x</td>
      <td class="num muted">${{p.liq?g4(p.liq):"—"}}</td>`;
    b.appendChild(tr);
  }});
}}

function heatmap(){{
  const wrap=document.getElementById('heatWrap'), hint=document.getElementById('heatHint');wrap.innerHTML="";
  if(!sel){{hint.innerHTML="Select a coin (Positioning tab or the search box) to see its liquidation heatmap.";return;}}
  const px=D.prices[sel];
  const liqs=D.positions.filter(p=>p.coin===sel && p.liq>0);
  if(!px||!liqs.length){{hint.innerHTML=`No liquidation-price data for <b>${{sel}}</b>.`;return;}}
  hint.innerHTML=`<b>${{sel}}</b> liquidation clusters · current price <b style="color:var(--amber)">${{g4(px)}}</b>`;
  const lv=liqs.map(p=>p.liq);
  let lo=Math.max(Math.min(...lv),px*0.5), hi=Math.min(Math.max(...lv),px*2.0);
  if(hi<=lo){{lo=px*0.7;hi=px*1.3;}}
  const N=26, step=(hi-lo)/N||1;
  const bins=Array.from({{length:N}},()=>({{L:0,S:0}}));
  liqs.forEach(p=>{{let i=Math.min(N-1,Math.max(0,Math.floor((p.liq-lo)/step)));(p.side==="long"?bins[i].L+=p.ntl:bins[i].S+=p.ntl);}});
  const nowBin=Math.min(N-1,Math.max(0,Math.floor((px-lo)/step)));
  const maxv=Math.max(...bins.map(b=>b.L+b.S),1);
  let html="";
  for(let i=N-1;i>=0;i--){{
    const b=bins[i], lvl=lo+step*(i+0.5), tot=b.L+b.S;
    const w=Math.round(100*tot/maxv);
    const cls=b.S>=b.L?"hmS":"hmL";
    html+=`<div class="hmrow${{i===nowBin?' hmnow':''}}"><span class="hmpx num">${{g4(lvl)}}</span>
      <span class=hmtrack><span class="hmbar ${{cls}}" style="width:${{w}}%"></span></span>
      <span class="hmval num">${{tot>0?usd(tot):""}}</span></div>`;
    if(i===nowBin) html+=`<div class=nowline><span class="hmpx num">◄ ${{g4(px)}}</span><span>current price</span></div>`;
  }}
  wrap.innerHTML=html;
}}

function setTab(t){{tab=t;
  document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('active',e.dataset.t===t));
  document.querySelectorAll('.pane').forEach(e=>e.classList.remove('active'));
  document.getElementById('pane-'+t).classList.add('active');}}
function copyAddr(a,el){{navigator.clipboard.writeText(a).then(()=>{{const o=el.textContent;el.textContent="✓";el.classList.add('ok');setTimeout(()=>{{el.textContent=o;el.classList.remove('ok');}},1000);}});}}
function pick(coin){{sel=coin;
  document.getElementById('search').value=coin;
  document.getElementById('chip').innerHTML = coin?`<span class=chip><b>${{coin}}</b> selected <span class=x onclick="openCoin('')">✕</span></span>`:"";
  // detail tab: show prompt when nothing selected, body when a coin is picked
  document.getElementById('detailPrompt').innerHTML = coin?"":"Click a coin in the Positioning tab (or search above) to see its position book and liquidation heatmap.";
  document.getElementById('detailBody').style.display = coin?"":"none";
  coinRows();bookRows();heatmap();}}
// select a coin AND jump to its detail view (or back to overview when cleared)
function openCoin(coin){{pick(coin);setTab(coin?'detail':'pos');}}
document.querySelectorAll('.tab').forEach(e=>e.onclick=()=>setTab(e.dataset.t));
document.getElementById('search').addEventListener('input',e=>{{const v=e.target.value.trim().toUpperCase();
  openCoin(D.coins.some(c=>c.coin===v)?v:"");}});
setTab('pos');pick('');
</script>
</div></body></html>"""

    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(OUT, "w") as f:
        f.write(html)
    print(f"wrote {OUT}  ({len(coins)} coins, {nwhales} whales, {len(positions)} positions, {len(prices)} prices)")


if __name__ == "__main__":
    render()
