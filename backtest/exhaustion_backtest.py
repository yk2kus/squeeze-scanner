#!/usr/bin/env python3
"""
Honest backtest of the EXHAUSTION reversal signal.

Question: after a sharp pump with a volume spike and fading momentum
(the components of the SqueezeScanner EXHAUST score), does price actually
REVERSE DOWN over the next few bars more than chance?

Data: FuturesScan daily panel (786 Binance USDT-perps, ~2 years). Daily bars
so fees are negligible relative to moves -- if it fails here, it isn't a
fee problem, it's a signal problem.

No lookahead: every feature uses data up to and including bar t; the outcome
is the return from close[t] to close[t+H].

Includes a NULL TEST: shuffle the score within each day and re-measure. If the
real result isn't clearly better than the shuffled one, the "signal" is noise.
"""
import numpy as np
import pandas as pd

PANEL = "/home/ubuntu/Desktop/Projects/trading-lab/FuturesScan/data/panel.parquet"
HORIZONS = [1, 3, 5]          # forward days to measure the "reversal"
FEE_BPS = 10.0               # round-trip cost assumption for a short (generous/low)


def zscore(s):
    m = s.rolling(60, min_periods=20).mean()
    sd = s.rolling(60, min_periods=20).std()
    return (s - m) / sd.replace(0, np.nan)


def build(df):
    df = df.sort_values(["symbol", "date"]).copy()
    g = df.groupby("symbol", group_keys=False)

    # --- EXHAUST components (all use info up to bar t only) ---
    # 1) large rapid gain: 3-bar run-up
    df["gain_3d"] = g["close"].apply(lambda c: c / c.shift(3) - 1.0)
    # 2) volume spike vs the coin's own 20-bar average
    df["vol_avg20"] = g["quote_volume"].apply(lambda v: v.rolling(20, min_periods=5).mean())
    df["vol_mult"] = df["quote_volume"] / df["vol_avg20"]
    # 3) momentum fading: today closed in the LOWER part of its range after the run
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    df["close_pos"] = (df["close"] - df["low"]) / rng        # 1=closed at high, 0=at low
    df["upper_wick"] = (df["high"] - df[["open", "close"]].max(axis=1)) / rng
    # 4) today red after being up
    df["today_ret"] = g["close"].apply(lambda c: c / c.shift(1) - 1.0)

    # normalise into 0..1-ish pieces
    df["z_gain"] = zscore(df["gain_3d"]).clip(-3, 3)
    df["z_vol"] = zscore(df["vol_mult"]).clip(-3, 3)

    # EXHAUST score: pumped hard (z_gain high) + volume spike (z_vol high)
    # + fading (closed low in range / red today). Higher = more "exhausted top".
    df["exhaust"] = (
        1.0 * df["z_gain"].clip(lower=0)          # only counts if genuinely up
        + 1.0 * df["z_vol"].clip(lower=0)         # only counts on a volume spike
        + 2.0 * (0.5 - df["close_pos"]).clip(lower=0) * 2  # closed in lower half
        + 1.0 * (df["today_ret"] < 0).astype(float)       # red today
    )

    # --- outcomes: forward returns (no lookahead) ---
    for h in HORIZONS:
        df[f"fwd_{h}"] = g["close"].apply(lambda c: c.shift(-h) / c - 1.0)
    return df


def report(df):
    print(f"rows usable: {len(df):,}   symbols: {df['symbol'].nunique()}   "
          f"dates: {df['date'].min()} -> {df['date'].max()}\n")

    # Focus on genuine pump bars (the only place an exhaustion short makes sense):
    # top-decile 3-day gain AND a volume spike.
    pump = df[(df["gain_3d"] > df["gain_3d"].quantile(0.90)) & (df["vol_mult"] > 2)].copy()
    print(f"pump-bar universe (top-decile 3d gain & vol>2x): {len(pump):,} events\n")

    # Within pumps, does a HIGH exhaust score predict a bigger DOWN move?
    hi = pump[pump["exhaust"] > pump["exhaust"].quantile(0.70)]
    lo = pump[pump["exhaust"] < pump["exhaust"].quantile(0.30)]

    print("=== Forward return after a PUMP bar, by exhaust score ===")
    print(f"{'H':>3} | {'ALL pumps':>12} | {'HIGH exhaust':>12} | {'LOW exhaust':>12} | "
          f"{'short EV (hi, net fee)':>22} | {'short win%':>10} | {'IC':>7} | {'null IC':>8}")
    for h in HORIZONS:
        col = f"fwd_{h}"
        a = pump[col].mean()
        hm = hi[col].mean()
        lm = lo[col].mean()
        # a short profits from a fall: pnl = -fwd - fee
        short_ev = (-hi[col].mean()) - FEE_BPS / 1e4
        short_win = (hi[col] < 0).mean()
        # IC: corr(exhaust, fwd) across all pump bars. Negative => higher score, lower fwd.
        sub = pump[["exhaust", col]].dropna()
        ic = sub["exhaust"].corr(sub[col]) if len(sub) > 30 else np.nan
        # null: shuffle exhaust, recompute IC (avg of 50 shuffles)
        nulls = []
        rng = np.random.default_rng(0)
        ev = sub[col].values
        ex = sub["exhaust"].values
        for _ in range(50):
            nulls.append(np.corrcoef(rng.permutation(ex), ev)[0, 1])
        null_ic = np.mean(np.abs(nulls))
        print(f"{h:>3} | {a*100:>11.2f}% | {hm*100:>11.2f}% | {lm*100:>11.2f}% | "
              f"{short_ev*100:>21.2f}% | {short_win*100:>9.1f}% | {ic:>7.3f} | {null_ic:>8.3f}")

    print("\nHow to read this:")
    print("- For the exhaustion SHORT to work: HIGH exhaust fwd return should be")
    print("  clearly NEGATIVE, more negative than LOW exhaust, and 'short EV' > 0.")
    print("- IC should be clearly NEGATIVE and its magnitude >> 'null IC' (shuffled).")
    print("- If HIGH-exhaust forward returns are ~0/positive, or |IC| ~ null IC,")
    print("  the exhaustion reversal signal is noise -- same result as the others.")


if __name__ == "__main__":
    raw = pd.read_parquet(PANEL)
    df = build(raw)
    df = df.dropna(subset=["exhaust", "gain_3d", "vol_mult", f"fwd_{HORIZONS[0]}"])
    report(df)
