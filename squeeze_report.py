#!/usr/bin/env python3
"""Render v2 squeeze.db (live phases + events eval) -> reports/squeeze.html."""
import sqlite3, html, datetime as dt
from pathlib import Path
import os
ROOT=Path(__file__).resolve().parent
DB=Path(os.getenv("DB_PATH") or str(ROOT/"squeeze.db"))
OUT=Path(os.getenv("REPORTS_DIR") or str(ROOT/"reports"))/"squeeze.html"
OUT.parent.mkdir(exist_ok=True)
PH={"STARTING":("🟢","#3fb950"),"ACTIVE":("🟠","#f0883e"),"EXHAUSTED":("🔴","#f85149"),
    "FAILED":("⚪","#8b949e")}
def q(c,sql,a=()):
    try: return c.execute(sql,a).fetchall()
    except Exception: return []
def main():
    now=dt.datetime.now(dt.timezone.utc)
    live=recent=ev_stats=[]; n_ev=n_active=0
    if DB.exists():
        c=sqlite3.connect(DB)
        live=q(c,"SELECT symbol,phase,start_score,exhaust_score,price_from_start,oi_from_start,liq_mult,vol_mult,taker_pct,funding,updated FROM live ORDER BY CASE phase WHEN 'ACTIVE' THEN 0 WHEN 'STARTING' THEN 1 WHEN 'EXHAUSTED' THEN 2 ELSE 3 END, updated DESC")
        n_ev=q(c,"SELECT count(*) FROM events")[0][0]
        closed=q(c,"SELECT count(*) FROM events WHERE closed=1")[0][0]
        act=q(c,"SELECT count(*) FROM events WHERE reached_active=1")[0][0]
        # forward-return stats on closed events
        rows=q(c,"SELECT p5m,p15m,p60m,mfe,mae,reached_active,final_phase FROM events WHERE closed=1")
        n_active=act
    def lrow(r):
        sym,ph,ss,ex,pf,oif,lm,vm,tk,fund,up=r
        emo,col=PH.get(ph,("•","#8b949e"))
        return (f"<tr><td>{emo} <b>{sym}</b></td><td style='color:{col};font-weight:700'>{ph}</td>"
                f"<td>{ss}/10</td><td>{ex}/10</td>"
                f"<td class='{'pos' if pf>0 else 'neg'}'>{pf:+.2f}%</td>"
                f"<td class='{'neg' if oif<0 else 'mut'}'>{oif:+.2f}%</td>"
                f"<td>{lm:.1f}x</td><td>{vm:.1f}x</td><td>{tk:.0f}%</td>"
                f"<td class='{'neg' if fund<0 else 'mut'}'>{fund*100:+.3f}%</td></tr>")
    live_html="".join(lrow(r) for r in live) or "<tr><td colspan=10 class=mut>No active squeeze right now — scanning 528 perps. Phases appear here as they fire.</td></tr>"
    # evaluation block
    ev_html=""
    if DB.exists():
        c=sqlite3.connect(DB)
        cl=q(c,"SELECT p5m,p15m,p60m,mfe,mae,reached_active FROM events WHERE closed=1")
        if cl:
            import statistics as stx
            def share(idx,thr):
                v=[r[idx] for r in cl if r[idx] is not None]
                return (sum(1 for x in v if x>=thr)/len(v)*100) if v else 0
            mfe=[r[3] for r in cl if r[3] is not None]; mae=[r[4] for r in cl if r[4] is not None]
            act_rate=sum(1 for r in cl if r[5])/len(cl)*100
            ev_html=(f"<div class=sub>Based on <b>{len(cl)}</b> matured events (60-min forward window):</div>"
                     f"<div class=cards>"
                     f"<div class=c><b>{share(0,1):.0f}%</b><span>+1% within 5m</span></div>"
                     f"<div class=c><b>{share(1,2):.0f}%</b><span>+2% within 15m</span></div>"
                     f"<div class=c><b>{share(2,5):.0f}%</b><span>+5% within 60m</span></div>"
                     f"<div class=c><b>{stx.median(mfe) if mfe else 0:+.1f}%</b><span>median max favorable</span></div>"
                     f"<div class=c><b>{stx.median(mae) if mae else 0:+.1f}%</b><span>median max adverse</span></div>"
                     f"<div class=c><b>{act_rate:.0f}%</b><span>reached ACTIVE</span></div>"
                     f"</div>")
        else:
            ev_html="<div class=sub>No events have matured yet (each needs a 60-min forward window). Stats appear here once STARTING signals accumulate.</div>"
    # HISTORY — every detected squeeze persists in the events table
    hist_html=""
    if DB.exists():
        c=sqlite3.connect(DB)
        rows=q(c,"""SELECT symbol,start_time,start_score,reached_active,final_phase,
                    mfe,mae,p60m,closed FROM events ORDER BY start_time DESC LIMIT 50""")
        def hrow(r):
            sym,t0,ss,act,ph,mfe,mae,p60,closed=r
            when=dt.datetime.fromtimestamp(t0,dt.timezone.utc).strftime("%m-%d %H:%M")
            emo,col=PH.get(ph,("•","#8b949e"))
            mfe_s=f"{mfe:+.1f}%" if mfe is not None else "—"
            mae_s=f"{mae:+.1f}%" if mae is not None else "—"
            p60_s=(f"{p60:+.1f}%" if p60 is not None else ("tracking…" if not closed else "—"))
            return (f"<tr><td class=mut>{when}</td><td><b>{sym}</b></td>"
                    f"<td>{ss}/10</td><td>{'✓' if act else '—'}</td>"
                    f"<td style='color:{col}'>{emo} {ph}</td>"
                    f"<td class=pos>{mfe_s}</td><td class=neg>{mae_s}</td>"
                    f"<td class='{'pos' if (p60 or 0)>0 else 'mut'}'>{p60_s}</td></tr>")
        hist_html="".join(hrow(r) for r in rows) or "<tr><td colspan=8 class=mut>No squeezes detected yet.</td></tr>"
    HIST_SECTION=(f"<h2>History — detected squeezes (persisted)</h2>"
                  f"<table><tr><th>When (UTC)</th><th>Symbol</th><th>Start</th><th>Reached ACTIVE</th>"
                  f"<th>Final phase</th><th>Max up</th><th>Max down</th><th>+60m</th></tr>{hist_html}</table>")
    OUT.write_text(f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=60>
<title>Squeeze scanner v2</title><style>
body{{background:#0d1117;color:#e6edf3;font:14px/1.5 -apple-system,Segoe UI,sans-serif;margin:0;padding:26px}}
h1{{font-size:19px;margin:0}}h2{{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:#8b949e;margin:24px 0 8px}}
.sub{{color:#8b949e;font-size:12px;margin:6px 0 10px}}
.paper{{display:inline-block;background:#3a2a15;color:#e3a008;border:1px solid #e3a00840;padding:3px 10px;border-radius:6px;font-size:11px;font-weight:600}}
.stamp{{position:absolute;top:22px;right:26px;background:#15301f;color:#3fb950;border:1px solid #2ea04340;padding:7px 12px;border-radius:8px;font-size:12px}}
.cards{{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0}}.c{{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:10px 14px;min-width:120px}}
.c b{{font-size:19px;display:block}}.c span{{color:#8b949e;font-size:11px;text-transform:uppercase}}
table{{width:100%;border-collapse:collapse}}th{{text-align:left;font-size:11px;color:#8b949e;text-transform:uppercase;padding:7px 8px;border-bottom:1px solid #21262d}}
td{{padding:8px;border-bottom:1px solid #161b22}}.mut{{color:#8b949e}}.pos{{color:#3fb950}}.neg{{color:#f85149}}tr:hover td{{background:#0f1620}}
</style>
<div class=stamp>{now:%d %b %Y %H:%M:%S} UTC</div>
<h1>Squeeze scanner v2 <span class=paper>ALERT-ONLY — no orders</span></h1>
<div class=sub>State machine: 🟢 STARTING → 🟠 ACTIVE → 🔴 EXHAUSTED (⚪ FAILED). Adaptive per-coin baselines (liquidations/volume as ×normal). Auto-refreshes each minute.</div>
<h2>Live phases</h2>
<table><tr><th>Symbol</th><th>Phase</th><th>Start</th><th>Exh</th><th>Px from start</th><th>OI from start</th><th>Liq</th><th>Vol</th><th>Taker</th><th>Funding</th></tr>{live_html}</table>
<h2>Evaluation — what happens after STARTING?</h2>
{ev_html}
{HIST_SECTION}
<div class=sub>Total STARTING events recorded: {n_ev}. This is the honest test: until these accumulate, a high score is a resemblance, not a prediction.</div>
""")
    print(f"rendered — live phases {len(live)}, events {n_ev}")
if __name__=="__main__": main()
