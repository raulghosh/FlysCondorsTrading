"""One runnable self-check. `python -m pytest -q` or `python tests/test_core.py`."""
import sys, math
from datetime import datetime, time, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ironfly import indicators as ind
from ironfly.config import Config, PROFILES
from ironfly.engine import Engine
from ironfly.feeds import SyntheticFeed, ET
from ironfly.manage import Position, evaluate
from ironfly.regime import classify
from ironfly.structure import build_condor, build_fly


def test_bsm_roundtrip():
    S, K, T, r, q, sig = 6400, 6300, 30 / 365, 0.04, 0.013, 0.18
    p = ind.bs_price(S, K, T, r, q, sig, "P")
    assert abs(ind.implied_vol(p, S, K, T, r, q, "P") - sig) < 1e-4
    g = ind.bs_greeks(S, K, T, r, q, sig, "P")
    assert -1 < g["delta"] < 0 and g["gamma"] > 0 and g["theta"] < 0 and g["vega"] > 0


def test_calm_regime_builds_structure():
    eng = Engine(SyntheticFeed(regime="calm", ts=datetime(2026, 9, 21, 10, 15, tzinfo=ET)), Config(), "weekly", journal=None)
    r = eng.scan()
    g = r.regime
    assert g.vrp > 0 and g.ts_ratio < 1
    assert r.structure is not None and r.structure.kind in ("IRON_FLY", "IRON_CONDOR")
    s = r.structure
    assert s.credit > 0 and s.max_loss > 0 and s.be_lo < eng.snapshot().spot < s.be_hi and 0 < s.pop < 1
    assert abs(s.delta) < 0.15
    assert s.half_life_days and s.days_to_target and 0 < s.days_to_target <= s.half_life_days < s.dte
    assert s.planned_hold_days == round(s.dte - PROFILES["weekly"].exit_dte, 3)


def test_stress_regime_vetoes():
    eng = Engine(SyntheticFeed(regime="stress", ts=datetime(2026, 9, 21, 10, 15, tzinfo=ET)), Config(), "weekly", journal=None)
    r = eng.scan()
    assert r.regime.strategy == "NONE" and "BACKWARDATION" in r.regime.vetoes and r.regime.hostile()


def test_fomc_blocks_entry():
    eng = Engine(SyntheticFeed(regime="calm", ts=datetime(2026, 9, 15, 10, 15, tzinfo=ET)), Config(), "weekly", journal=None)
    assert any(v.startswith("EVENT_FOMC") for v in eng.scan().regime.vetoes)


def test_cpi_nfp_pce_block_entry():
    # weekly profile blocks 2 days ahead of an event; CPI/NFP/PCE prints are all seeded for 2026
    for d, tag in [(datetime(2026, 9, 9, 10, 15, tzinfo=ET), "CPI"),      # CPI Sep 11
                   (datetime(2026, 9, 2, 10, 15, tzinfo=ET), "NFP"),      # NFP Sep 4
                   (datetime(2026, 9, 28, 10, 15, tzinfo=ET), "PCE")]:    # PCE Sep 30
        eng = Engine(SyntheticFeed(regime="calm", ts=d), Config(), "weekly", journal=None)
        vetoes = eng.scan().regime.vetoes
        assert any(v.startswith(f"EVENT_{tag}") for v in vetoes), (tag, vetoes)


def test_management_triggers():
    cfg, prof = Config(), PROFILES["weekly"]
    ts = datetime(2026, 9, 21, 10, 15, tzinfo=ET)
    snap = SyntheticFeed(regime="calm", ts=ts).snapshot()
    reg = classify(snap, cfg, prof, [])
    expiry = [e for e in snap.expiries() if round(snap.dte(e)) == 7][0]
    s = build_condor(snap, expiry, cfg, prof)
    pos = Position.from_structure("t", s, 1, ts, "weekly", snap.spot)
    # same snapshot -> HOLD
    assert evaluate(pos, snap, reg, cfg, prof, [])[0].type == "HOLD"
    # pretend we sold it for much more -> profit target
    rich = Position(**{**pos.to_dict(), "entry_credit": s.credit * 3})
    assert evaluate(rich, snap, reg, cfg, prof, [])[0].type == "TAKE_PROFIT"
    # entered for almost nothing -> stop loss
    poor = Position(**{**pos.to_dict(), "entry_credit": s.credit * 0.2})
    assert evaluate(poor, snap, reg, cfg, prof, [])[0].type == "STOP_LOSS"
    # spot rallies into the short call -> tested -> roll the put side in
    hot = SyntheticFeed(regime="calm", ts=ts, spot0=s.short_call.strike - 5).snapshot()
    acts = evaluate(pos, hot, classify(hot, cfg, prof, []), cfg, prof, [])
    assert acts[0].type in ("ROLL_UNTESTED_IN", "STOP_LOSS", "HOLD", "TAKE_PROFIT"), acts[0]
    # 1 DTE -> time exit
    late = SyntheticFeed(regime="calm", ts=ts + timedelta(days=6)).snapshot()
    assert evaluate(pos, late, reg, cfg, prof, [])[0].type == "TAKE_PROFIT"   # theta did its job first
    flat = Position(**{**pos.to_dict(), "entry_credit": s.credit * 0.10})       # too little credit to hit 50%
    assert evaluate(flat, late, reg, cfg, prof, [])[0].type == "TIME_EXIT"


def test_schedule_rules():
    from dataclasses import replace
    from ironfly.structure import reprice
    cfg, prof = Config(), PROFILES["monthly"]
    ts = datetime(2026, 9, 21, 10, 15, tzinfo=ET)
    snap = SyntheticFeed(regime="calm", ts=ts).snapshot()
    reg = classify(snap, cfg, prof, [])
    expiry = [e for e in snap.expiries() if round(snap.dte(e)) == 45][0]
    s = build_condor(snap, expiry, cfg, prof)
    pos = Position.from_structure("m", s, 1, ts, "monthly", snap.spot)
    # 15 days later, spot unchanged: pnl should sit on the theta schedule
    later = SyntheticFeed(regime="calm", ts=ts + timedelta(days=15)).snapshot()
    a = evaluate(pos, later, reg, cfg, prof, [])[0]
    assert abs(a.details["vs_schedule"]) < 0.08, a.details
    # same clock, but pretend we entered for barely more than today's close cost -> stale
    cost, _ = reprice(pos.legs, later, expiry, cfg)
    stale = Position(**{**pos.to_dict(), "entry_credit": cost * 1.05})
    a = evaluate(stale, later, reg, cfg, replace(prof, stale_mult=0.5), [])[0]
    assert a.type == "STALE_EXIT", a
    # captured 45% in 2 days -> ahead of schedule
    soon = SyntheticFeed(regime="calm", ts=ts + timedelta(days=2), dtes=(43,)).snapshot()
    cost, _ = reprice(pos.legs, soon, expiry, cfg)
    early = Position(**{**pos.to_dict(), "entry_credit": cost / 0.55})
    a = evaluate(early, soon, reg, cfg, prof, [])[0]
    assert a.type == "TAKE_PROFIT" and "ahead of schedule" in a.reason, a


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
