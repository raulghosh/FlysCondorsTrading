"""Persist snapshots to JSON so live data accumulates for replay/backtest.
Brokers only serve live chains; the history you backtest on is the history you record."""
from __future__ import annotations
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .feeds import OptionQuote, Snapshot, ET


def save(snap: Snapshot, folder: str) -> Path:
    Path(folder).mkdir(parents=True, exist_ok=True)
    p = Path(folder) / f"{snap.ts:%Y%m%d_%H%M%S}.json"
    h = snap.history.round(4)
    h.index = [d.isoformat() for d in h.index.date]
    rec = {"ts": snap.ts.isoformat(), "spot": snap.spot, "vix": snap.vix, "vix_prev_close": snap.vix_prev_close,
           "vix3m": snap.vix3m, "vvix": snap.vvix, "vix9d": snap.vix9d, "source": snap.source,
           "history": h.to_dict("split"),
           "chain": [[o.expiry.isoformat(), o.strike, o.cp, round(o.bid, 2), round(o.ask, 2), round(o.iv, 3), round(o.delta, 4),
                      round(o.gamma, 6), round(o.theta, 4), round(o.vega, 4), o.oi, o.volume] for o in snap.chain]}
    p.write_text(json.dumps(rec))
    return p


def load(path: str | Path) -> Snapshot:
    r = json.loads(Path(path).read_text())
    h = pd.DataFrame(**r["history"])
    h.index = pd.to_datetime(h.index)
    chain = [OptionQuote(date.fromisoformat(c[0]), *c[1:]) for c in r["chain"]]
    return Snapshot(datetime.fromisoformat(r["ts"]).astimezone(ET), r["spot"], r["vix"], r["vix_prev_close"], chain, h,
                    r["vix3m"], r["vvix"], r["vix9d"], r["source"])


class ReplayFeed:
    """Iterates recorded snapshots in time order; snapshot() returns the latest (for `scan`/`manage`)."""

    def __init__(self, folder: str):
        self.paths = sorted(Path(folder).glob("*.json"))
        if not self.paths:
            raise RuntimeError(f"no snapshots in {folder}")

    def __iter__(self):
        for p in self.paths:
            yield load(p)

    def snapshot(self) -> Snapshot:
        return load(self.paths[-1])
