#!/usr/bin/env python3
"""
Broad, honest search for a REVERSAL edge.

We do NOT just test one definition. We sweep many:
  - direction: SHORT after a pump  AND  LONG after a dump
  - move lookback: 2/3/5 bars
  - volume filter: >1.5x / >2x / >3x
  - fade trigger: closed-low-in-range / red candle / big upper(lower) wick
  - hold horizon: 1/3/5 bars

The catch that keeps us honest:
  * Split by DATE into TRAIN (older half) and TEST (newer half).
  * Rank every variant by TRAIN net-EV per trade.
  * Then look at how the TRAIN winners do on TEST (never tuned on).
  * A real edge survives out-of-sample. Overfit noise does not.
  * Multiple-comparison aware: we try N variants, so ~N*p will look good on
    TRAIN by luck. The TEST column is the only one that matters.

No lookahead: features use bars <= t; outcome is close[t]->close[t+H].
Fees: round-trip FEE_BPS charged to every trade.
"""
import numpy as np
import pandas as pd
from itertools import product

PANEL = "/home/ubuntu/Desktop/Projects/trading-lab/FuturesScan/data/panel.parquet"
FEE_BPS = 10.0
MIN_TRADES = 300   # ignore variants too rare to trust


def prep(df):
    df = df.sort_values(["symbol", "date"]).copy()
    g = df.groupby("symbol", group_keys=False)
    for n in (2, 3, 5):
        df[f"gain_{n}"] = g["close"].apply(lambda c: c / c.shift(n) - 1.0)
    df["vol_avg20"] = g["quote_volume"].apply(lambda v: v.rolling(20, min_periods=5).mean())
    df["vol_mult"] = df["quote_volume"] / df["vol_avg20"]
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    df["close_pos"] = (df["close"] - df["low"]) / rng
    df["upper_wick"] = (df["high"] - df[["open", "close"]].max(axis=1)) / rng
    df["lower_wick"] = (df[["open", "close"]].min(axis=1) - df["low"]) / rng
    df["today_ret"] = g["close"].apply(lambda c: c / c.shift(1) - 1.0)
    for h in (1, 3, 5):
        df[f"fwd_{h}"] = g["close"].apply(lambda c: c.shift(-h) / c - 1.0)
    return df


def signal_mask(df, direction, look, volx, fade):
    if direction == "short":  # after a pump, expect a drop
        move = df[f"gain_{look}"] > df[f"gain_{look}"].quantile(0.90)
        if fade == "closelow":  f = df["close_pos"] < 0.4
        elif fade == "red":     f = df["today_ret"] < 0
        elif fade == "wick":    f = df["upper_wick"] > 0.4
    else:                      # long: after a dump, expect a bounce
        move = df[f"gain_{look}"] < df[f"gain_{look}"].quantile(0.10)
        if fade == "closelow":  f = df["close_pos"] > 0.6      # closed strong off lows
        elif fade == "red":     f = df["today_ret"] > 0        # green today
        elif fade == "wick":    f = df["lower_wick"] > 0.4     # long lower wick
    return move & (df["vol_mult"] > volx) & f


def net_ev(sub, direction, h):
    col = f"fwd_{h}"
    r = sub[col].dropna()
    if len(r) < MIN_TRADES:
        return None
    sign = -1.0 if direction == "short" else 1.0
    pnl = sign * r - FEE_BPS / 1e4
    return dict(n=len(r), ev=pnl.mean(), win=(pnl > 0).mean(), t=pnl.mean() / (pnl.std() / np.sqrt(len(pnl))))


def run(df):
    cut = df["date"].quantile(0.5)
    train = df[df["date"] <= cut]
    test = df[df["date"] > cut]
    print(f"TRAIN {train['date'].min()}..{train['date'].max()}  ({len(train):,} rows)")
    print(f"TEST  {test['date'].min()}..{test['date'].max()}  ({len(test):,} rows)\n")

    grid = list(product(["short", "long"], [2, 3, 5], [1.5, 2, 3],
                        ["closelow", "red", "wick"], [1, 3, 5]))
    rows = []
    for direction, look, volx, fade, h in grid:
        m_tr = signal_mask(train, direction, look, volx, fade)
        r_tr = net_ev(train[m_tr], direction, h)
        if r_tr is None:
            continue
        m_te = signal_mask(test, direction, look, volx, fade)
        r_te = net_ev(test[m_te], direction, h)
        rows.append(dict(dir=direction, look=look, volx=volx, fade=fade, h=h,
                         tr_ev=r_tr["ev"], tr_t=r_tr["t"], tr_n=r_tr["n"],
                         te_ev=(r_te["ev"] if r_te else np.nan),
                         te_t=(r_te["t"] if r_te else np.nan),
                         te_n=(r_te["n"] if r_te else np.nan)))
    res = pd.DataFrame(rows)
    print(f"variants tested (>= {MIN_TRADES} trades): {len(res)}\n")

    top = res.sort_values("tr_ev", ascending=False).head(12)
    print("=== Top 12 by TRAIN net-EV -- then how they do OUT-OF-SAMPLE (TEST) ===")
    print(f"{'dir':>5} {'look':>4} {'volx':>4} {'fade':>9} {'h':>2} | "
          f"{'TRAIN ev':>9} {'t':>6} {'n':>6} | {'TEST ev':>9} {'t':>6} {'n':>6}")
    for _, r in top.iterrows():
        print(f"{r['dir']:>5} {r['look']:>4} {r['volx']:>4} {r['fade']:>9} {int(r['h']):>2} | "
              f"{r['tr_ev']*100:>8.2f}% {r['tr_t']:>6.2f} {int(r['tr_n']):>6} | "
              f"{r['te_ev']*100:>8.2f}% {r['te_t']:>6.2f} "
              f"{(int(r['te_n']) if pd.notna(r['te_n']) else 0):>6}")

    # The honest scoreboard
    surv = res[(res["tr_ev"] > 0) & (res["tr_t"] > 2) & (res["te_ev"] > 0) & (res["te_t"] > 2)]
    print(f"\nVariants profitable AND t>2 on BOTH train and test: {len(surv)} / {len(res)}")
    if len(surv):
        print(surv.to_string(index=False))
    else:
        print("=> NONE survived out-of-sample. Every train 'winner' collapsed on unseen data.")

    # correlation of train-EV vs test-EV across all variants: does train success predict test?
    ok = res.dropna(subset=["te_ev"])
    c = ok["tr_ev"].corr(ok["te_ev"])
    print(f"\ncorr(TRAIN ev, TEST ev) across all {len(ok)} variants: {c:.3f}")
    print("(If ~0 or negative, doing well in-sample tells you NOTHING about the future.)")


if __name__ == "__main__":
    raw = pd.read_parquet(PANEL)
    df = prep(raw)
    run(df)
