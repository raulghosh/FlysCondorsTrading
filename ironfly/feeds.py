"""Market data adapters. Implement `MarketFeed.snapshot()` to plug in any broker.

Feeds never place orders. Credentials come from environment variables only:
  SCHWAB_ACCESS_TOKEN               (OAuth bearer, obtained by you)
  APCA_API_KEY_ID / APCA_API_SECRET_KEY
"""
from __future__ import annotations
import io
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, time
from typing import Protocol
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from . import indicators as ind

ET = ZoneInfo("America/New_York")
SETTLE_TIME = time(16, 0)  # PM-settled SPX weeklies. AM-settled monthlies are ~6.5h earlier; immaterial for DTE>=1.


@dataclass
class OptionQuote:
    expiry: date
    strike: float
    cp: str            # "C" | "P"
    bid: float
    ask: float
    iv: float          # vol points, e.g. 18.5
    delta: float
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    oi: int = 0
    volume: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid if self.mid > 0 else 9.9


@dataclass
class Snapshot:
    ts: datetime                 # tz-aware, ET
    spot: float
    vix: float
    vix_prev_close: float
    chain: list[OptionQuote]
    history: pd.DataFrame        # daily: open, high, low, close, vix (+ optional vix3m, vvix, vix9d)
    vix3m: float | None = None
    vvix: float | None = None
    vix9d: float | None = None
    source: str = ""

    def expiries(self) -> list[date]:
        return sorted({q.expiry for q in self.chain})

    def dte(self, expiry: date) -> float:
        exp_dt = datetime.combine(expiry, SETTLE_TIME, tzinfo=ET)
        return max((exp_dt - self.ts).total_seconds() / 86400.0, 0.0)


class MarketFeed(Protocol):
    def snapshot(self) -> Snapshot: ...


def fill_greeks(chain: list[OptionQuote], snap_ts: datetime, spot: float, r: float, q: float) -> None:
    """Compute missing greeks/IV with BSM. Feeds that already supply them are left alone."""
    for o in chain:
        T = max((datetime.combine(o.expiry, SETTLE_TIME, tzinfo=ET) - snap_ts).total_seconds(), 0) / (365 * 86400)
        if not o.iv or math.isnan(o.iv):
            o.iv = ind.implied_vol(o.mid, spot, o.strike, T, r, q, o.cp) * 100
        if math.isnan(o.iv):
            o.iv = 0.0
            o.delta = o.gamma = o.theta = o.vega = 0.0
            continue
        if not o.delta or (o.gamma == 0 and o.vega == 0):
            g = ind.bs_greeks(spot, o.strike, T, r, q, o.iv / 100, o.cp)
            o.delta, o.gamma, o.theta, o.vega = g["delta"], g["gamma"], g["theta"], g["vega"]


# ---------------- CBOE public index history (free, no key) ----------------

def cboe_index_history(symbol: str) -> pd.Series:
    """Daily closes for VIX, VIX3M, VIX9D, VVIX from CBOE's public CSVs."""
    url = f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{symbol}_History.csv"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip().lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")["close"].rename(symbol.lower())


# ---------------- CSV replay ----------------

class CSVFeed:
    """history.csv: date,open,high,low,close,vix[,vix3m,vvix,vix9d]
       chain.csv:   expiry,strike,cp,bid,ask[,iv,delta,gamma,theta,vega,oi,volume]
       Spot = last close of history unless `spot` given."""

    def __init__(self, folder: str, ts: datetime | None = None, spot: float | None = None, r=0.04, q=0.013):
        self.folder, self.ts, self.spot, self.r, self.q = folder, ts, spot, r, q

    def snapshot(self) -> Snapshot:
        h = pd.read_csv(f"{self.folder}/history.csv", parse_dates=["date"]).set_index("date").sort_index()
        c = pd.read_csv(f"{self.folder}/chain.csv", parse_dates=["expiry"])
        ts = self.ts or datetime.combine(h.index[-1].date(), time(10, 0), tzinfo=ET)
        spot = self.spot or float(h["close"].iloc[-1])
        chain = [OptionQuote(row.expiry.date(), float(row.strike), row.cp, float(row.bid), float(row.ask),
                             float(getattr(row, "iv", float("nan")) or float("nan")),
                             float(getattr(row, "delta", 0.0) or 0.0), float(getattr(row, "gamma", 0.0) or 0.0),
                             float(getattr(row, "theta", 0.0) or 0.0), float(getattr(row, "vega", 0.0) or 0.0),
                             int(getattr(row, "oi", 0) or 0), int(getattr(row, "volume", 0) or 0))
                 for row in c.itertuples()]
        fill_greeks(chain, ts, spot, self.r, self.q)
        g = lambda col: float(h[col].iloc[-1]) if col in h else None
        return Snapshot(ts, spot, float(h["vix"].iloc[-1]), float(h["vix"].iloc[-2]), chain, h,
                        g("vix3m"), g("vvix"), g("vix9d"), source="csv")


# ---------------- Schwab ----------------

class SchwabFeed:
    """Schwab Trader API market data. SPX index options + $VIX family quotes + price history."""
    BASE = "https://api.schwabapi.com/marketdata/v1"

    def __init__(self, token: str | None = None, symbol: str = "$SPX", max_dte: int = 60, r=0.04, q=0.013):
        self.token = token or os.environ.get("SCHWAB_ACCESS_TOKEN")
        if not self.token:
            raise RuntimeError("Set SCHWAB_ACCESS_TOKEN (OAuth bearer token from your Schwab developer app).")
        self.symbol, self.max_dte, self.r, self.q = symbol, max_dte, r, q
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {self.token}"

    def _get(self, path, **params):
        resp = self.s.get(f"{self.BASE}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def snapshot(self) -> Snapshot:
        now = datetime.now(ET)
        qs = self._get("/quotes", symbols="$SPX,$VIX,$VIX3M,$VVIX,$VIX9D")
        last = lambda k: float(qs[k]["quote"]["lastPrice"]) if k in qs else None
        prev = lambda k: float(qs[k]["quote"].get("closePrice", qs[k]["quote"]["lastPrice"])) if k in qs else None
        spot = last("$SPX")
        hist = self._history("$SPX")
        vix_h = self._history("$VIX")["close"].rename("vix")
        hist = hist.join(vix_h, how="left")
        for sym, col in (("$VIX3M", "vix3m"), ("$VVIX", "vvix"), ("$VIX9D", "vix9d")):
            try:
                hist = hist.join(self._history(sym)["close"].rename(col), how="left")
            except Exception:
                pass
        chain = self._chain(now)
        fill_greeks(chain, now, spot, self.r, self.q)
        return Snapshot(now, spot, last("$VIX"), prev("$VIX"), chain, hist,
                        last("$VIX3M"), last("$VVIX"), last("$VIX9D"), source="schwab")

    def _history(self, sym) -> pd.DataFrame:
        j = self._get("/pricehistory", symbol=sym, periodType="year", period=2, frequencyType="daily", frequency=1)
        df = pd.DataFrame(j["candles"])
        df["date"] = pd.to_datetime(df["datetime"], unit="ms").dt.normalize()
        return df.set_index("date")[["open", "high", "low", "close"]]

    def _chain(self, now: datetime) -> list[OptionQuote]:
        j = self._get("/chains", symbol=self.symbol, contractType="ALL", strategy="SINGLE",
                      fromDate=now.date().isoformat(), toDate=(now.date() + timedelta(days=self.max_dte)).isoformat())
        out = []
        for key, cp in (("putExpDateMap", "P"), ("callExpDateMap", "C")):
            for exp_key, strikes in j.get(key, {}).items():
                exp = date.fromisoformat(exp_key.split(":")[0])
                for k, lst in strikes.items():
                    o = lst[0]
                    iv = float(o.get("volatility") or 0)
                    out.append(OptionQuote(exp, float(k), cp, float(o["bid"]), float(o["ask"]),
                                           iv if iv > 0 else float("nan"), float(o.get("delta") or 0),
                                           float(o.get("gamma") or 0), float(o.get("theta") or 0),
                                           float(o.get("vega") or 0), int(o.get("openInterest") or 0),
                                           int(o.get("totalVolume") or 0)))
        return out


# ---------------- Alpaca ----------------

_OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


class AlpacaFeed:
    """Alpaca options data. Alpaca lists equity/ETF options, not SPX index options, so this
    uses SPY (spot x10 ~ SPX) by default. VIX family comes from CBOE's public CSVs.
    Fine for regime + strike selection; execute the SPX equivalent yourself."""
    DATA = "https://data.alpaca.markets"

    def __init__(self, key=None, secret=None, underlying="SPY", max_dte=60, r=0.04, q=0.013):
        key = key or os.environ.get("APCA_API_KEY_ID")
        secret = secret or os.environ.get("APCA_API_SECRET_KEY")
        if not (key and secret):
            raise RuntimeError("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY.")
        self.underlying, self.max_dte, self.r, self.q = underlying, max_dte, r, q
        self.s = requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def _get(self, path, **params):
        resp = self.s.get(f"{self.DATA}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def snapshot(self) -> Snapshot:
        now = datetime.now(ET)
        start = (now - timedelta(days=800)).date().isoformat()
        bars = self._get(f"/v2/stocks/{self.underlying}/bars", timeframe="1Day", start=start, limit=10000, adjustment="split")["bars"]
        hist = pd.DataFrame(bars).rename(columns={"o": "open", "h": "high", "l": "low", "c": "close"})
        hist["date"] = pd.to_datetime(hist["t"]).dt.tz_localize(None).dt.normalize()
        hist = hist.set_index("date")[["open", "high", "low", "close"]]
        spot = float(self._get(f"/v2/stocks/{self.underlying}/trades/latest")["trade"]["p"])
        vix = cboe_index_history("VIX")
        hist = hist.join(vix, how="left")
        extra = {}
        for sym in ("VIX3M", "VVIX", "VIX9D"):
            try:
                s = cboe_index_history(sym)
                hist = hist.join(s, how="left")
                extra[sym.lower()] = float(s.iloc[-1])
            except Exception:
                extra[sym.lower()] = None
        hist["vix"] = hist["vix"].ffill()
        chain = self._chain(now)
        fill_greeks(chain, now, spot, self.r, self.q)
        return Snapshot(now, spot, float(vix.iloc[-1]), float(vix.iloc[-2]), chain, hist,
                        extra["vix3m"], extra["vvix"], extra["vix9d"], source="alpaca")

    def _chain(self, now: datetime) -> list[OptionQuote]:
        out, token = [], None
        while True:
            params = dict(feed="indicative", limit=1000, expiration_date_gte=now.date().isoformat(),
                          expiration_date_lte=(now.date() + timedelta(days=self.max_dte)).isoformat())
            if token:
                params["page_token"] = token
            j = self._get(f"/v1beta1/options/snapshots/{self.underlying}", **params)
            for sym, snap in j.get("snapshots", {}).items():
                m = _OCC.match(sym)
                q = snap.get("latestQuote") or {}
                if not m or not q:
                    continue
                exp = datetime.strptime(m.group(2), "%y%m%d").date()
                g = snap.get("greeks") or {}
                iv = snap.get("impliedVolatility")
                out.append(OptionQuote(exp, int(m.group(4)) / 1000, m.group(3), float(q.get("bp", 0)), float(q.get("ap", 0)),
                                       float(iv) * 100 if iv else float("nan"), float(g.get("delta") or 0),
                                       float(g.get("gamma") or 0), float(g.get("theta") or 0), float(g.get("vega") or 0)))
            token = j.get("next_page_token")
            if not token:
                return out


# ---------------- Synthetic (demo / tests / backtest) ----------------

def _gbm_history(rng, n: int, end: date, spot_end: float, regime: str = "calm") -> pd.DataFrame:
    """GBM with vol clustering; VIX = RV20 x 1.25 + noise (positive VRP); contango unless stress."""
    vol = np.empty(n); vol[0] = 0.14
    rets = np.empty(n)
    for i in range(n):
        if i:
            vol[i] = 0.93 * vol[i - 1] + 0.07 * 0.14 + 0.02 * rng.standard_normal() * (1 + 3 * (rets[i - 1] < -0.01))
            vol[i] = float(np.clip(vol[i], 0.08, 0.6))
        rets[i] = rng.standard_normal() * vol[i] / math.sqrt(252) + 0.0003
    close = np.exp(np.cumsum(rets))
    close = close / close[-1] * spot_end
    opn = close * np.exp(rng.standard_normal(n) * vol / math.sqrt(252) * 0.3)
    high = np.maximum(opn, close) * (1 + np.abs(rng.standard_normal(n)) * vol / math.sqrt(252) * 0.5)
    low = np.minimum(opn, close) * (1 - np.abs(rng.standard_normal(n)) * vol / math.sqrt(252) * 0.5)
    rv = pd.Series(np.log(close)).diff().rolling(20).std().bfill().values * math.sqrt(252) * 100
    vix = rv * 1.25 + rng.standard_normal(n) * 0.8 + 1.0
    if regime == "stress":
        vix = vix * 1.8; ts_ratio, vvix = 1.08, 140.0
    else:
        ts_ratio, vvix = 0.92, 92.0
    idx = pd.bdate_range(end=end, periods=n + 5)[-n:]
    return pd.DataFrame({"open": opn, "high": high, "low": low, "close": close, "vix": vix, "vix3m": vix / ts_ratio,
                         "vvix": vvix, "vix9d": vix * (1.05 if regime == "stress" else 0.95)}, index=idx)


def _bsm_chain(rng, spot: float, atm_iv: float, ts: datetime, expiries: list[date], r: float, q: float) -> list[OptionQuote]:
    """Strikes every 5 pts, 85-115% of spot; put skew + call smile; OI heavier on round strikes."""
    chain = []
    for exp in expiries:
        d = (exp - ts.date()).days
        T = max((datetime.combine(exp, SETTLE_TIME, tzinfo=ET) - ts).total_seconds(), 60) / (365 * 86400)
        for k in np.arange(round(spot * 0.85 / 5) * 5, spot * 1.15, 5.0):
            m = (spot - k) / spot
            iv = atm_iv * (1 + 2.2 * m) if m >= 0 else atm_iv * (1 - 0.6 * m + 4 * m * m)
            iv *= (1 + 0.15 * (d == 0))
            for cp in "PC":
                px = ind.bs_price(spot, k, T, r, q, iv / 100, cp)
                half = max(0.05, 0.02 * px + 0.1 * (d == 0))
                oi = int(rng.integers(50, 3000)) * (8 if k % 50 == 0 else 1)
                chain.append(OptionQuote(exp, float(k), cp, max(px - half, 0.0), px + half, iv, 0.0, oi=oi))
    fill_greeks(chain, ts, spot, r, q)
    return chain


class SyntheticFeed:
    """One snapshot: GBM history, contango VIX, BSM chain with put skew. Deterministic by seed."""

    def __init__(self, seed=7, days=420, spot0=6400.0, atm_iv=18.0, regime="calm", ts: datetime | None = None,
                 dtes=(0, 1, 2, 7, 14, 30, 45), r=0.04, q=0.013):
        self.seed, self.days, self.spot0, self.atm_iv, self.regime, self.dtes, self.r, self.q = seed, days, spot0, atm_iv, regime, dtes, r, q
        self.ts = ts

    def snapshot(self) -> Snapshot:
        rng = np.random.default_rng(self.seed)
        ts = self.ts or datetime.combine(date.today(), time(10, 15), tzinfo=ET)
        hist = _gbm_history(rng, self.days, ts.date(), self.spot0, self.regime)
        vix = hist["vix"].to_numpy().copy()
        vix[-15:] += np.linspace(0, self.atm_iv - vix[-1], 15)   # recent vol pickup so IVR is mid-range
        hist["vix"] = vix; hist["vix3m"] = vix / (1.08 if self.regime == "stress" else 0.92)
        hist["vix9d"] = vix * (1.05 if self.regime == "stress" else 0.95)
        spot = float(hist["close"].iloc[-1])
        atm = float(vix[-1]) if self.regime == "stress" else self.atm_iv
        chain = _bsm_chain(rng, spot, atm, ts, [ts.date() + timedelta(days=d) for d in self.dtes], self.r, self.q)
        return Snapshot(ts, spot, float(vix[-1]), float(vix[-2]), chain, hist, float(hist["vix3m"].iloc[-1]),
                        float(hist["vvix"].iloc[-1]), float(hist["vix9d"].iloc[-1]), source=f"synthetic:{self.regime}")


class SyntheticPath:
    """Iterable of one 10:15 ET snapshot per business day along a single simulated path, with real
    Friday expiries so positions can be carried and managed across days. For backtest demos.
    ponytail: indicators see the day's close at 10:15 (mild lookahead); irrelevant for a synthetic demo."""

    def __init__(self, seed=7, warmup=420, days=120, spot0=6400.0, end: date | None = None, r=0.04, q=0.013):
        self.seed, self.warmup, self.days, self.spot0, self.end, self.r, self.q = seed, warmup, days, spot0, end, r, q

    def __iter__(self):
        rng = np.random.default_rng(self.seed)
        n = self.warmup + self.days
        hist = _gbm_history(rng, n, self.end or date.today(), self.spot0)
        vix = hist["vix"].to_numpy().copy()
        mean60 = pd.Series(vix).rolling(60).mean().bfill().values
        stressed = vix > 1.4 * mean60                      # vol spike days: backwardation + VVIX up
        hist["vix3m"] = np.where(stressed, vix / 1.05, vix / 0.92)
        hist["vvix"] = np.where(stressed, 135.0, 92.0)
        hist["vix9d"] = np.where(stressed, vix * 1.04, vix * 0.95)
        for i in range(self.warmup, n):
            day = hist.index[i].date()
            ts = datetime.combine(day, time(10, 15), tzinfo=ET)
            h = hist.iloc[: i + 1]
            spot = float(h["close"].iloc[-1])
            # SPX lists Mon/Wed/Fri weeklies ~5 weeks out, Fridays beyond that
            exps = [day + timedelta(days=k) for k in range(0, 61)
                    if (day + timedelta(days=k)).weekday() in ((0, 2, 4) if k <= 35 else (4,))]
            chain = _bsm_chain(rng, spot, float(vix[i]), ts, exps, self.r, self.q)
            yield Snapshot(ts, spot, float(vix[i]), float(vix[i - 1]), chain, h, float(h["vix3m"].iloc[-1]),
                           float(h["vvix"].iloc[-1]), float(h["vix9d"].iloc[-1]), source="synthetic:path")
