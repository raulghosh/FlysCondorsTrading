"""Pure math: Black-Scholes (European, continuous dividend), realized vol,
IV rank, term structure, trend/range measures. numpy/pandas/scipy only."""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
from scipy.stats import norm
from scipy.optimize import brentq

YEAR = 365.0


def _d1d2(S, K, T, r, q, sig):
    T = max(T, 1e-8)
    sig = max(sig, 1e-6)
    d1 = (math.log(S / K) + (r - q + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return d1, d1 - sig * math.sqrt(T)


def bs_price(S, K, T, r, q, sig, cp: str) -> float:
    if T <= 0:
        return max(0.0, S - K) if cp == "C" else max(0.0, K - S)
    d1, d2 = _d1d2(S, K, T, r, q, sig)
    if cp == "C":
        return S * math.exp(-q * T) * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * math.exp(-q * T) * norm.cdf(-d1)


def bs_greeks(S, K, T, r, q, sig, cp: str) -> dict:
    """delta, gamma, theta (per day), vega (per vol point)."""
    if T <= 0:
        itm = (S > K) if cp == "C" else (S < K)
        return {"delta": (1.0 if cp == "C" else -1.0) if itm else 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    d1, d2 = _d1d2(S, K, T, r, q, sig)
    eq, er, sq = math.exp(-q * T), math.exp(-r * T), math.sqrt(T)
    pdf = norm.pdf(d1)
    gamma = eq * pdf / (S * sig * sq)
    vega = S * eq * pdf * sq / 100.0
    if cp == "C":
        delta = eq * norm.cdf(d1)
        theta = -S * eq * pdf * sig / (2 * sq) - r * K * er * norm.cdf(d2) + q * S * eq * norm.cdf(d1)
    else:
        delta = eq * (norm.cdf(d1) - 1.0)
        theta = -S * eq * pdf * sig / (2 * sq) + r * K * er * norm.cdf(-d2) - q * S * eq * norm.cdf(-d1)
    return {"delta": delta, "gamma": gamma, "theta": theta / YEAR, "vega": vega}


def implied_vol(price, S, K, T, r, q, cp: str) -> float:
    intrinsic = bs_price(S, K, 0, r, q, 0, cp)
    if T <= 0 or price <= intrinsic + 1e-9:
        return float("nan")
    try:
        return brentq(lambda s: bs_price(S, K, T, r, q, s, cp) - price, 1e-4, 5.0, xtol=1e-6)
    except ValueError:
        return float("nan")


def prob_between(S, lo, hi, sig, T, r=0.0, q=0.0) -> float:
    """Lognormal P(lo < S_T < hi)."""
    if T <= 0:
        return float(lo < S < hi)
    mu = (r - q - 0.5 * sig * sig) * T
    sd = sig * math.sqrt(T)
    z = lambda x: (math.log(x / S) - mu) / sd
    return norm.cdf(z(hi)) - norm.cdf(z(lo))


# ---------- time series ----------

def realized_vol_cc(close: pd.Series, window: int) -> pd.Series:
    """Close-to-close, annualized, in vol points (e.g. 15.2)."""
    return np.log(close).diff().rolling(window).std() * math.sqrt(252) * 100


def yang_zhang(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Yang-Zhang OHLC estimator, annualized vol points. Needs open/high/low/close."""
    o = np.log(df["open"] / df["close"].shift(1))
    c = np.log(df["close"] / df["open"])
    h = np.log(df["high"] / df["open"])
    l = np.log(df["low"] / df["open"])
    rs = (h * (h - c) + l * (l - c)).rolling(window).mean()
    k = 0.34 / (1.34 + (window + 1) / (window - 1))
    var = o.rolling(window).var() + k * c.rolling(window).var() + (1 - k) * rs
    return np.sqrt(var.clip(lower=0) * 252) * 100


def iv_rank(series: pd.Series, lookback: int = 252) -> tuple[float, float]:
    """(IV rank, IV percentile) of the last value vs the trailing window, both 0..100."""
    s = series.dropna().iloc[-lookback:]
    cur = s.iloc[-1]
    lo, hi = s.min(), s.max()
    rank = 0.0 if hi == lo else (cur - lo) / (hi - lo) * 100
    pct = (s < cur).mean() * 100
    return float(rank), float(pct)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = atr(df, n)
    pdi = 100 * pd.Series(plus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / tr
    mdi = 100 * pd.Series(minus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def sma_zscore(df: pd.DataFrame, n: int = 20) -> float:
    """Distance of last close from SMA(n) in ATR(14) units."""
    close = df["close"]
    return float((close.iloc[-1] - close.rolling(n).mean().iloc[-1]) / atr(df).iloc[-1])


def bollinger_width_pct(close: pd.Series, n: int = 20) -> float:
    """Current BB width / its trailing 1y percentile (0..100). Low = squeeze."""
    m = close.rolling(n).mean()
    sd = close.rolling(n).std()
    w = (4 * sd / m).dropna()
    cur = w.iloc[-1]
    return float((w.iloc[-252:] < cur).mean() * 100)


def expected_move(S: float, iv_pct: float, dte_days: float) -> float:
    """1-sigma expected move in points."""
    return S * iv_pct / 100 * math.sqrt(max(dte_days, 0.25) / YEAR)
