"""CLI.  python -m ironfly demo | scan | manage | paper-open | show-config"""
from __future__ import annotations
import argparse, json, sys
from datetime import datetime
from pathlib import Path

from .config import Config, PROFILES
from .engine import Engine
from .feeds import CSVFeed, SchwabFeed, AlpacaFeed, SyntheticFeed
from .manage import Position


def make_feed(a):
    if a.feed == "synthetic":
        return SyntheticFeed(regime=a.regime)
    if a.feed == "csv":
        return CSVFeed(a.data)
    if a.feed == "schwab":
        return SchwabFeed()
    if a.feed == "alpaca":
        return AlpacaFeed()
    raise SystemExit(f"unknown feed {a.feed}")


def load_cfg(path):
    return Config.from_json(Path(path).read_text()) if path and Path(path).exists() else Config()


def load_positions(path) -> list[Position]:
    p = Path(path)
    return [Position(**d) for d in json.loads(p.read_text())] if p.exists() else []


def print_scan(r):
    g = r.regime
    print(f"== {r.ts}  profile={r.profile}  source scan")
    print(f"strategy: {g.strategy}   fly={g.fly_score:.2f} condor={g.condor_score:.2f}")
    print(f"IVR {g.ivr:.0f} / IVP {g.ivp:.0f}  VIX {g.vix:.1f}  RV20 {g.rv20:.1f}  YZ20 {g.yz20:.1f}  VRP {g.vrp:+.1f}"
          f"  VIX/VIX3M {g.ts_ratio}  9d/30d {g.ts9_ratio}  VVIX {g.vvix}")
    print(f"ADX {g.adx}  SMA20-z {g.sma_z}  BBwidth%ile {g.bb_width_pct}  skew25 {g.skew25}  ATM IV {g.atm_iv}  1sd move ±{g.expected_move}")
    if g.vetoes:   print("VETOES:  ", ", ".join(g.vetoes))
    if g.warnings: print("WARNINGS:", ", ".join(g.warnings))
    if g.events:   print("events:  ", ", ".join(g.events))
    if r.kill:     print("KILL:    ", "; ".join(r.kill))
    s = r.structure
    if s:
        legs = "  ".join(f"{'-' if l.qty<0 else '+'}{l.strike:.0f}{l.cp}" for l in s.legs)
        print(f"\n{s.kind} {s.expiry} (dte {s.dte:.1f}):  {legs}")
        print(f"credit {s.credit:.2f}  width {s.width_put:.0f}/{s.width_call:.0f}  credit/width {s.credit_to_width:.2f}  "
              f"max loss {s.max_loss:.2f} (${s.max_loss*100:,.0f}/ct)  BE {s.be_lo:.0f}-{s.be_hi:.0f}  POP {s.pop:.0%}  P(touch) {s.p_touch_short:.0%}")
        print(f"greeks/ct: delta {s.delta*100:+.1f} gamma {s.gamma*100:.4f} theta {s.theta*100:+.1f}/day vega {s.vega*100:+.1f}")
        print(f"timing: half-life {s.half_life_days}d  expected days to target {s.days_to_target}d  planned hold {s.planned_hold_days}d"
              f"  (stale after {s.days_to_target * PROFILES[r.profile].stale_mult if s.days_to_target else '-'}d)")
        print(f"size: {r.contracts} contracts   ({'; '.join(r.sizing_notes)})")
        for n in s.notes: print("note:", n)
    print("\n>>> ENTRY SIGNAL" if r.entry_signal() else "\n>>> NO ENTRY")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ironfly")
    ap.add_argument("cmd", choices=["demo", "scan", "manage", "paper-open", "show-config"])
    ap.add_argument("--feed", default="synthetic", choices=["synthetic", "csv", "schwab", "alpaca"])
    ap.add_argument("--regime", default="calm", choices=["calm", "stress"], help="synthetic feed only")
    ap.add_argument("--data", default="data", help="folder for csv feed")
    ap.add_argument("--profile", default="weekly", choices=list(PROFILES))
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--positions", default="positions.json")
    ap.add_argument("--daily-pnl", type=float, default=0.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "show-config":
        print(Config().to_json()); return
    cfg = load_cfg(a.config)
    eng = Engine(make_feed(a), cfg, a.profile)
    positions = load_positions(a.positions)

    if a.cmd in ("scan", "demo"):
        r = eng.scan(positions, a.daily_pnl)
        print(json.dumps(r.to_dict(), indent=1, default=str)) if a.json else print_scan(r)
        if a.cmd == "demo" and r.structure:
            pos = Position.from_structure("demo-1", r.structure, max(r.contracts, 1), eng.snapshot().ts, a.profile, eng.snapshot().spot)
            positions = [pos]
            print("\n-- managing the position just built (same snapshot, so expect HOLD) --")
    if a.cmd == "paper-open":
        r = eng.scan(positions, a.daily_pnl)
        if not r.entry_signal():
            print_scan(r); raise SystemExit("no entry signal; nothing opened")
        pid = f"{a.profile}-{datetime.now():%Y%m%d-%H%M}"
        positions.append(Position.from_structure(pid, r.structure, r.contracts, eng.snapshot().ts, a.profile, eng.snapshot().spot))
        Path(a.positions).write_text(json.dumps([p.to_dict() for p in positions], indent=1))
        print(f"recorded paper position {pid} in {a.positions}"); return
    if a.cmd in ("manage", "demo"):
        if not positions:
            print("no positions in", a.positions); return
        for pid, acts in eng.manage(positions).items():
            for x in acts:
                print(f"[{pid}] {x.urgency:4} {x.type:18} {x.reason}")
                if a.json: print(json.dumps(x.details, indent=1, default=str))


if __name__ == "__main__":
    main()
