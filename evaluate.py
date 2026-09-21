import sqlite3, pandas as pd, numpy as np

con=sqlite3.connect("squeeze.db")
df=pd.read_sql_query("SELECT * FROM observations ORDER BY symbol,ts",con)
con.close()

if df.empty:
    print("No data yet.")
    raise SystemExit

df["future_5m"]=np.nan
df["future_15m"]=np.nan
df["future_60m"]=np.nan

for sym,g in df.groupby("symbol"):
    g=g.sort_values("ts")
    p=g["price"].values; t=g["ts"].values
    for idx,row in g.iterrows():
        target=row.ts
        for col,mins in [("future_5m",5),("future_15m",15),("future_60m",60)]:
            future=target+mins*60
            j=np.searchsorted(t,future)
            if j<len(p) and row.price:
                df.loc[idx,col]=(p[j]/row.price-1)*100

signals=df[df.score>=7]
print("Observations:",len(df))
print("Score >=7 signals:",len(signals))
if len(signals):
    print(signals[["future_5m","future_15m","future_60m"]].describe().round(3))
    print("\nPositive forward-return rate:")
    for c in ["future_5m","future_15m","future_60m"]:
        print(c, round((signals[c]>0).mean()*100,2), "%")
print("\nNOTE: This is an in-sample descriptive check. Do not treat it as proof of predictive performance.")
