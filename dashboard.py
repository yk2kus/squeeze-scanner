import sqlite3, pandas as pd, streamlit as st
from datetime import datetime, timezone

DB="squeeze.db"
st.set_page_config(page_title="Binance Squeeze Scanner", layout="wide")
st.title("Binance Futures — Short Squeeze Monitor")
st.caption("Research/alert dashboard. Scores are heuristic, not trade recommendations.")

try:
    con=sqlite3.connect(DB)
    df=pd.read_sql_query("SELECT * FROM observations ORDER BY ts DESC LIMIT 5000",con)
    con.close()
except Exception:
    df=pd.DataFrame()

if df.empty:
    st.info("No observations yet. Start scanner.py first.")
    st.stop()

df["time"]=pd.to_datetime(df.ts,unit="s",utc=True)
latest=df.sort_values("ts").groupby("symbol").tail(1).sort_values("score",ascending=False)

st.metric("Symbols with observations", latest.symbol.nunique())
st.metric("Latest high-score signals", int((latest.score>=7).sum()))

st.subheader("Current leaders")
st.dataframe(latest[["time","symbol","price","funding","score","reasons"]].head(50),
             use_container_width=True, hide_index=True)

symbols=st.multiselect("Chart symbols", sorted(df.symbol.unique()),
                       default=list(latest.symbol.head(3)))
for sym in symbols:
    x=df[df.symbol==sym].sort_values("ts").tail(500)
    st.subheader(sym)
    st.line_chart(x.set_index("time")[["score"]])
    st.line_chart(x.set_index("time")[["price"]])
    st.line_chart(x.set_index("time")[["oi"]])
