"""Replay snapshots through the engine, act on its signals mechanically, tally the result.
Fills at mid less the configured slippage; one entry per day; one position per expiry."""
from __future__ import annotations
import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Iterable

from .config import Config, PROFILES
from .engine import Engine
from .feeds import Snapshot
from .manage import Position
from .structure import _nearest

CLOSE = {"TAKE_PROFIT", "STOP_LOSS", "TIME_EXIT", "REGIME_EXIT", "STALE_EXIT", "EVENT_EXIT"}


@dataclass
class Trade:
    id: str
    kind: str
    expiry: str
    entry_ts: str
    exit_ts: str
    entry_credit: float
    exit_cost: float
    contracts: int
    pnl_usd: float
    exit_reason: str
    days_held: float
    days_to_target: float | None
    adjustments: int


@dataclass
class Report:
    trades: list[Trade]
    scans: int
    entries_signalled: int
    blocked: Counter          # why scans did not become entries: veto names, structure notes, size 0

    def summary(self) -> dict:
        t = self.trades
        if not t:
            return {"trades": 0}
        pnl = [x.pnl_usd for x in t]
        eq, peak, dd = 0.0, 0.0, 0.0
        for p in pnl:
            eq += p; peak = max(peak, eq); dd = min(dd, eq - peak)
        by = defaultdict(list)
        for x in t:
            by[x.exit_reason].append(x.pnl_usd)
        wins = [p for p in pnl if p > 0]
        return {"trades": len(t), "win_rate": round(len(wins) / len(t), 3), "total_pnl": round(sum(pnl)),
                "avg_pnl": round(sum(pnl) / len(t)), "avg_win": round(sum(wins) / len(wins)) if wins else 0,
                "avg_loss": round(sum(p for p in pnl if p <= 0) / max(len(t) - len(wins), 1)),
                "max_drawdown": round(dd), "avg_days_held": round(sum(x.days_held for x in t) / len(t), 1),
                "avg_days_to_target": round(sum(x.days_to_target or 0 for x in t) / len(t), 1),
                "by_exit": {k: {"n": len(v), "avg": round(sum(v) / len(v)), "total": round(sum(v))} for k, v in sorted(by.items())},
                "scans": self.scans, "entries_signalled": self.entries_signalled,
                "blocked": dict(self.blocked.most_common())}

    def to_csv(self, path: str) -> None:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(asdict(self.trades[0]).keys()) if self.trades else ["id"])
            w.writeheader()
            for t in self.trades:
                w.writerow(asdict(t))


def _close(pos: Position, snap: Snapshot, cost: float, reason: str) -> Trade:
    held = (snap.ts - datetime.fromisoformat(pos.entry_ts)).total_seconds() / 86400
    return Trade(pos.id, pos.kind, pos.expiry, pos.entry_ts, snap.ts.isoformat(), round(pos.entry_credit, 2), round(cost, 2),
                 pos.contracts, round((pos.entry_credit - cost) * 100 * pos.contracts), reason, round(held, 2),
                 pos.days_to_target, pos.adjustments)


def _from_struct_dict(pid: str, d: dict, contracts: int, snap: Snapshot, profile: str, adjustments: int) -> Position:
    return Position(pid, d["kind"], d["expiry"], [{k: l[k] for k in ("cp", "strike", "qty", "iv", "price")} for l in d["legs"]],
                    d["credit"], contracts, snap.ts.isoformat(), profile, adjustments, entry_spot=snap.spot,
                    half_life_days=d.get("half_life_days"), days_to_target=d.get("days_to_target"),
                    planned_hold_days=d.get("planned_hold_days"))


def _settle_expired(pos: Position, snap: Snapshot) -> float:
    """Intrinsic value at settlement for a position that outlived its last snapshot before expiry."""
    return sum(-l["qty"] * (max(snap.spot - l["strike"], 0) if l["cp"] == "C" else max(l["strike"] - snap.spot, 0)) for l in pos.legs)


def run(snaps: Iterable[Snapshot], cfg: Config | None = None, profile: str = "weekly") -> Report:
    cfg = cfg or Config()
    eng = Engine(None, cfg, profile, journal=None)
    positions: list[Position] = []
    trades: list[Trade] = []
    scans = signalled = seq = 0
    blocked: Counter = Counter()
    last_entry_day: date | None = None
    for snap in snaps:
        eng.set_snapshot(snap)
        # expiry settlement for anything the exit rules did not catch (sparse snapshots)
        for pos in list(positions):
            if snap.ts.date() > date.fromisoformat(pos.expiry):
                trades.append(_close(pos, snap, _settle_expired(pos, snap), "EXPIRED")); positions.remove(pos)
        for pid, acts in eng.manage(positions).items():
            pos = next(p for p in positions if p.id == pid)
            for a in acts:
                d = a.details
                if a.type in CLOSE:
                    trades.append(_close(pos, snap, d["close_cost"], a.type)); positions.remove(pos); break
                if a.type == "TIGHTEN_STOP":
                    pos.stop_mult_override = d["new_stop_mult"]
                elif a.type == "ROLL_UNTESTED_IN":
                    exp = date.fromisoformat(pos.expiry)
                    closed = {(l["cp"], l["strike"], l["qty"]) for l in d["close"]}
                    pos.legs = [l for l in pos.legs if (l["cp"], l["strike"], l["qty"]) not in closed]
                    for l in d["open"]:
                        q = _nearest(snap.chain, exp, l["cp"], l["strike"])
                        pos.legs.append({**l, "iv": q.iv, "price": q.mid})
                    pos.entry_credit += d["net_credit"]; pos.adjustments += 1
                    pos.entry_spot = snap.spot   # schedule is re-based on the new structure
                    pos.notes.append(f"{snap.ts.date()} {a.type} {d['net_credit']:+.2f}")
                    break
                elif a.type in ("ROLL_OUT_IN_TIME", "RECENTER_FLY"):
                    trades.append(_close(pos, snap, d["close_cost"], a.type)); positions.remove(pos)
                    if d.get("new_structure"):
                        seq += 1
                        positions.append(_from_struct_dict(f"{pos.id}r{seq}", d["new_structure"], pos.contracts, snap, pos.profile, pos.adjustments + 1))
                    break
        if last_entry_day != snap.ts.date():
            scans += 1
            r = eng.scan(positions)
            if not r.entry_signal():
                g = r.regime
                why = (r.kill or g.vetoes or ([f"score<{cfg.min_score}"] if g.strategy == "NONE" else [])
                       or [n.split(":")[0] for n in (r.structure.notes if r.structure else ["NO_STRUCTURE"])]
                       or (["SIZE_0"] if r.contracts == 0 else []) or ["?"])
                blocked[why[0]] += 1
            if r.entry_signal():
                signalled += 1
                if all(p.expiry != r.structure.expiry.isoformat() for p in positions):
                    seq += 1
                    positions.append(Position.from_structure(f"bt{seq}", r.structure, r.contracts, snap.ts, profile, snap.spot))
                    last_entry_day = snap.ts.date()
    return Report(trades, scans, signalled, blocked)


def print_report(rep: Report) -> None:
    s = rep.summary()
    if s["trades"] == 0:
        print(f"no closed trades ({rep.scans} scans, {rep.entries_signalled} entry signals)")
        _print_blocked(rep); return
    print(f"trades {s['trades']}  win rate {s['win_rate']:.0%}  total ${s['total_pnl']:,}  avg ${s['avg_pnl']:,}  "
          f"avg win ${s['avg_win']:,}  avg loss ${s['avg_loss']:,}  max DD ${s['max_drawdown']:,}")
    print(f"avg days held {s['avg_days_held']}  vs expected days to target {s['avg_days_to_target']}  "
          f"({rep.scans} scans, {rep.entries_signalled} entry signals)")
    for k, v in s["by_exit"].items():
        print(f"  {k:18} n={v['n']:3}  avg ${v['avg']:>7,}  total ${v['total']:>8,}")
    _print_blocked(rep)


def _print_blocked(rep: Report) -> None:
    if rep.blocked:
        print("scans without entry, by first blocker:")
        for k, n in rep.blocked.most_common(8):
            print(f"  {k:40} {n}")
