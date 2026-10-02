#!/usr/bin/env python3
"""
Attack the 'buy-the-dump hammer' result before believing it.

Three killers, in order of likelihood:
  1. SURVIVORSHIP: panel only has coins that still exist. If nearly every
     symbol trades right up to the panel's end date, the delisted/dead coins
     were removed -> 'buy the dump' is inflated because the dumps that never
     recovered are missing.
  2. BULL-MARKET BETA: if the whole market rose, buying any dip wins. Re-test
     the edge MARKET-NEUTRAL (return minus that day's cross-sectional mean).
  3. ONE-REGIME LUCK: break the edge down year by year. A real edge shows up
     in most years; an artifact is one or two bull years.
"""
import numpy as np
import pandas as pd

PANEL = "/home/ubuntu/Desktop/Projects/trading-lab/FuturesScan/data/panel.parquet"
FEE_BPS = 10.0


def prep(df):
    df = df.sort_values(["symbol", "date"]).copy()
    df["date"] = pd.to_datetime(df["date"])
    g = df.groupby("symbol", group_keys=False)
    df["gain_3"] = g["close"].apply(lambda c: c / c.shift(3) - 1.0)
    df["vol_avg20"] = g["quote_volume"].apply(lambda v: v.rolling(20, min_periods=5).mean())
    df["vol_mult"] = df["quote_volume"] / df["vol_avg20"]
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    df["lower_wick"] = (df[["open", "close"]].min(axis=1) - df["low"]) / rng
    df["fwd_3"] = g["close"].apply(lambda c: c.shift(-3) / c - 1.0)
    # market-neutral fwd: subtract that day's equal-weight mean forward return
    day_mean = df.groupby("date")["fwd_3"].transform("mean")
    df["fwd_3_mkt"] = df["fwd_3"] - day_mean
    return df


def main():
    raw = pd.read_parquet(PANEL)
    df = prep(raw)
    panel_end = df["date"].max()

    # ---- 1) SURVIVORSHIP ----
    last = df.groupby("symbol")["date"].max()
    frac_alive = (last >= panel_end - pd.Timedelta(days=7)).mean()
    print("=== 1) SURVIVORSHIP CHECK ===")
    print(f"panel end date: {panel_end.date()}   symbols: {df['symbol'].nunique()}")
    print(f"fraction of symbols still trading within 7d of panel end: {frac_alive*100:.1f}%")
    print("(If ~all symbols run to the end, delisted/dead coins were dropped ->")
    print(" survivorship bias present, and 'buy the dump' is inflated.)\n")
    # how many symbols 'died' (stopped) mid-panel?
    died = (last < panel_end - pd.Timedelta(days=30)).sum()
    print(f"symbols that stopped >30d before end (possible delistings kept): {died}\n")

    # the winning signal: 3-day dump (bottom decile), vol>2x, long lower wick, hold 3d
    m = (df["gain_3"] < df["gain_3"].quantile(0.10)) & (df["vol_mult"] > 2) & (df["lower_wick"] > 0.4)
    sig = df[m].dropna(subset=["fwd_3"])
    print(f"signal fires: {len(sig)} trades\n")

    # ---- 2) MARKET-NEUTRAL ----
    raw_pnl = sig["fwd_3"] - FEE_BPS / 1e4
    mkt_pnl = sig["fwd_3_mkt"] - FEE_BPS / 1e4
    def line(name, p):
        t = p.mean() / (p.std() / np.sqrt(len(p)))
        print(f"{name:>22}: EV {p.mean()*100:>7.2f}%  win {(p>0).mean()*100:>5.1f}%  t {t:>6.2f}  n {len(p)}")
    print("=== 2) RAW vs MARKET-NEUTRAL (does it survive removing market beta?) ===")
    line("raw (long dip)", raw_pnl)
    line("market-neutral", mkt_pnl)
    base = df["fwd_3"].dropna()
    print(f"{'baseline (all bars)':>22}: EV {base.mean()*100:>7.2f}%  (the market's own drift)\n")

    # ---- 3) YEAR BY YEAR (market-neutral) ----
    print("=== 3) YEAR-BY-YEAR (market-neutral EV -- real edge shows in most years) ===")
    sig = sig.copy()
    sig["year"] = sig["date"].dt.year
    for y, grp in sig.groupby("year"):
        p = grp["fwd_3_mkt"] - FEE_BPS / 1e4
        if len(p) < 20:
            print(f"  {y}: n={len(p)} (too few)")
            continue
        t = p.mean() / (p.std() / np.sqrt(len(p)))
        print(f"  {y}: EV {p.mean()*100:>7.2f}%  win {(p>0).mean()*100:>5.1f}%  t {t:>6.2f}  n {len(p)}")


if __name__ == "__main__":
    main()
