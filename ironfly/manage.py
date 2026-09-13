"""Per-position lifecycle: profit/stop/time exits, tested-side detection, adjustment and
roll proposals. Produces Actions; a human (or your own execution layer) acts on them."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta

from .config import Config, DTEProfile
from .events import blocking_events
from .feeds import Snapshot
from .regime import Regime, by_delta, pick_expiry
from .structure import Structure, build_condor, build_fly, reprice, _nearest, assemble, expected_close_cost

# urgency: NOW = act this bar, ACT = act today, INFO = watch
@dataclass
class Action:
    type: str
    urgency: str
    reason: str
    details: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class Position:
    id: str
    kind: str
    expiry: str                 # ISO
    legs: list[dict]            # {cp, strike, qty, iv, price}  (iv/price at entry; optional for hand-entered positions)
    entry_credit: float         # points per unit
    contracts: int
    entry_ts: str
    profile: str
    adjustments: int = 0
    stop_mult_override: float | None = None
    notes: list[str] = field(default_factory=list)
    entry_spot: float | None = None
    half_life_days: float | None = None
    days_to_target: float | None = None
    planned_hold_days: float | None = None

    @classmethod
    def from_structure(cls, pid: str, s: Structure, contracts: int, ts: datetime, profile: str, spot: float | None = None) -> "Position":
        return cls(pid, s.kind, s.expiry.isoformat(),
                   [{"cp": l.cp, "strike": l.strike, "qty": l.qty, "iv": l.iv, "price": l.price} for l in s.legs],
                   s.credit, contracts, ts.isoformat(), profile, entry_spot=spot,
                   half_life_days=s.half_life_days, days_to_target=s.days_to_target, planned_hold_days=s.planned_hold_days)

    def schedule(self, now: datetime, dte_now: float, cfg: Config) -> dict | None:
        """Where the trade should be on its own theta curve right now. None if entry ivs unknown."""
        if self.entry_spot is None or any("iv" not in l or "price" not in l for l in self.legs):
            return None
        days_held = (now - datetime.fromisoformat(self.entry_ts)).total_seconds() / 86400
        gross = sum(-l["qty"] * l["price"] for l in self.legs)
        cost = expected_close_cost(self.legs, self.entry_spot, [l["iv"] for l in self.legs], dte_now / 365, cfg)
        return {"days_held": round(days_held, 2), "expected_pnl_frac": round((gross - cost) / gross, 3),
                "half_life_days": self.half_life_days, "days_to_target": self.days_to_target,
                "planned_hold_days": self.planned_hold_days}

    def to_dict(self):
        return asdict(self)


def _propose_roll_untested(pos: Position, snap: Snapshot, expiry: date, cfg: Config, profile: DTEProfile,
                           tested_cp: str, width: float) -> dict | None:
    """Roll the untested short (and its wing) closer, to `untested_roll_delta`, for a net credit."""
    un_cp = "C" if tested_cp == "P" else "P"
    old_short = next(l for l in pos.legs if l["cp"] == un_cp and l["qty"] < 0)
    old_long = next(l for l in pos.legs if l["cp"] == un_cp and l["qty"] > 0)
    new_s = by_delta(snap.chain, expiry, un_cp, profile.untested_roll_delta)
    if new_s is None:
        return None
    new_l = _nearest(snap.chain, expiry, un_cp, new_s.strike - width if un_cp == "P" else new_s.strike + width)
    if new_l is None:
        return None
    close_cost, _ = reprice([old_short, old_long], snap, expiry, cfg)
    open_credit = new_s.mid - new_l.mid - 2 * cfg.slippage_per_leg
    net = open_credit - close_cost
    return {"side": un_cp, "close": [old_short, old_long],
            "open": [{"cp": un_cp, "strike": new_s.strike, "qty": -1}, {"cp": un_cp, "strike": new_l.strike, "qty": 1}],
            "net_credit": round(float(net), 2), "new_short_delta": round(float(abs(new_s.delta)), 3)}


def _propose_roll_out(pos: Position, snap: Snapshot, cfg: Config, profile: DTEProfile, close_cost: float) -> dict | None:
    """Close and re-open the same kind of structure in the next expiry in profile, only for net credit."""
    cur = date.fromisoformat(pos.expiry)
    later = [e for e in snap.expiries() if e > cur and profile.min_dte <= round(snap.dte(e)) <= profile.max_dte + 7]
    if not later:
        return None
    nxt = later[0]
    new = build_condor(snap, nxt, cfg, profile) if pos.kind == "IRON_CONDOR" else build_fly(snap, nxt, cfg, profile)
    if new is None:
        return None
    net = new.credit - close_cost
    return {"new_expiry": nxt.isoformat(), "new_structure": new.to_dict(), "close_cost": round(float(close_cost), 2),
            "net_credit": round(float(net), 2)}


def evaluate(pos: Position, snap: Snapshot, regime: Regime, cfg: Config, profile: DTEProfile, events: list[dict]) -> list[Action]:
    expiry = date.fromisoformat(pos.expiry)
    dte = snap.dte(expiry)
    close_cost, legs = reprice(pos.legs, snap, expiry, cfg)
    pnl_pts = pos.entry_credit - close_cost
    pnl_frac = pnl_pts / pos.entry_credit if pos.entry_credit else 0.0
    pnl_usd = pnl_pts * 100 * pos.contracts
    sp = next(l for l in legs if l.cp == "P" and l.qty < 0)
    sc = next(l for l in legs if l.cp == "C" and l.qty < 0)
    net_delta = sum(l.qty * l.delta for l in legs) * 100 * pos.contracts
    width = max(sp.strike - next(l.strike for l in legs if l.cp == "P" and l.qty > 0),
                next(l.strike for l in legs if l.cp == "C" and l.qty > 0) - sc.strike)
    is_fly = pos.kind == "IRON_FLY"
    target = profile.fly_profit_target if is_fly else profile.condor_profit_target
    stop_mult = pos.stop_mult_override or (profile.fly_stop_mult if is_fly else profile.condor_stop_mult)
    tested_cp = "P" if abs(sp.delta) >= profile.tested_delta else ("C" if abs(sc.delta) >= profile.tested_delta else None)
    breached = snap.spot < sp.strike or snap.spot > sc.strike
    hhmm = snap.ts.strftime("%H:%M")
    m = {"pnl_pts": round(float(pnl_pts), 2), "pnl_frac": round(float(pnl_frac), 3), "pnl_usd": round(float(pnl_usd)),
         "close_cost": round(float(close_cost), 2), "dte": round(dte, 2), "spot": snap.spot, "short_put": sp.strike,
         "short_call": sc.strike, "short_put_delta": round(float(sp.delta), 3), "short_call_delta": round(float(sc.delta), 3),
         "net_delta": round(float(net_delta), 1), "tested": tested_cp, "breached": bool(breached)}
    acts: list[Action] = []

    # 1. hard exits first
    if pnl_frac >= target:
        return [Action("TAKE_PROFIT", "NOW", f"{pnl_frac:.0%} of credit captured (target {target:.0%})", m)]
    if pnl_frac <= -stop_mult:
        return [Action("STOP_LOSS", "NOW", f"loss {-pnl_frac:.2f}x credit >= stop {stop_mult}x", m)]
    if profile.target_dte == 0 and hhmm >= profile.exit_time:
        return [Action("TIME_EXIT", "NOW", f"0DTE hard exit time {profile.exit_time} reached; gamma too high into the close", m)]
    if profile.target_dte > 0 and dte < profile.exit_dte + 1:   # calendar-day semantics: "the 21-DTE day"
        return [Action("TIME_EXIT", "ACT", f"DTE {dte:.1f} <= {profile.exit_dte}: gamma/theta ratio no longer favourable", m)]
    if breached and regime.adx >= cfg.breach_adx:
        return [Action("STOP_LOSS", "NOW", f"short strike breached with ADX {regime.adx:.0f} >= {cfg.breach_adx}: trending, do not fight", m)]

    # 2. schedule: is theta paying on time?
    sch = pos.schedule(snap.ts, dte, cfg)
    if sch:
        m.update(sch)
        m["vs_schedule"] = round(pnl_frac - sch["expected_pnl_frac"], 3)
        held, d2t, plan = sch["days_held"], sch["days_to_target"], sch["planned_hold_days"]
        if d2t and pnl_frac >= profile.early_take_frac * target and held <= profile.early_time_frac * d2t:
            return [Action("TAKE_PROFIT", "NOW", f"ahead of schedule: {pnl_frac:.0%} captured in {held:.1f}d, "
                           f"expected {d2t:.1f}d for {target:.0%}. Take it; remaining edge is tiny vs gamma", m)]
        if d2t and held >= profile.stale_mult * d2t and pnl_frac < target:
            return [Action("STALE_EXIT", "ACT", f"held {held:.1f}d, {profile.stale_mult}x the expected {d2t:.1f}d to target, "
                           f"still only {pnl_frac:+.0%}: theta is not paying, redeploy capital", m)]
        if plan and held >= 0.5 * plan and pnl_frac < profile.lag_tolerance * sch["expected_pnl_frac"]:
            acts.append(Action("LAGGING", "INFO", f"{pnl_frac:+.0%} vs {sch['expected_pnl_frac']:+.0%} expected at day {held:.1f}: "
                               "spot or IV moved against the trade; do not add, lean toward the first exit that triggers", m))

    # 3. environment-driven
    if regime.hostile():
        if pnl_pts > 0:
            return [Action("REGIME_EXIT", "NOW", f"regime hostile {regime.vetoes}; book the gain", m)]
        acts.append(Action("TIGHTEN_STOP", "ACT", f"regime hostile {regime.vetoes}; stop tightened to {stop_mult/2:.2f}x credit",
                           {**m, "new_stop_mult": stop_mult / 2}))
    ev = blocking_events(snap.ts.date(), 1, events)
    if ev and dte <= 2 and profile.target_dte > 0:
        acts.append(Action("EVENT_EXIT", "ACT", f"{ev[0]['name']} on {ev[0]['date']} inside the last 2 DTE; close or halve", m))

    # 4. tested side -> adjust / roll / exit
    if tested_cp:
        can_adjust = pos.adjustments < profile.max_adjustments and dte > profile.roll_min_dte
        un = sc if tested_cp == "P" else sp
        if not can_adjust:
            acts.append(Action("STOP_LOSS", "ACT", f"{tested_cp} side tested (delta {sp.delta if tested_cp=='P' else sc.delta:+.2f}) "
                               f"and no adjustments left (used {pos.adjustments}/{profile.max_adjustments}, dte {dte:.1f}); exit", m))
        elif is_fly:
            drift = abs(snap.spot - sp.strike) / width
            if drift >= profile.fly_recenter_frac and regime.strategy == "IRON_FLY":
                new = build_fly(snap, expiry, cfg, profile)
                acts.append(Action("RECENTER_FLY", "ACT", f"spot drifted {drift:.0%} of wing from body; regime still fly-friendly",
                                   {**m, "new_structure": new.to_dict() if new else None, "close_cost": round(close_cost, 2)}))
            else:
                ro = _propose_roll_out(pos, snap, cfg, profile, close_cost)
                if ro and ro["net_credit"] >= 0 and not regime.vetoes:
                    acts.append(Action("ROLL_OUT_IN_TIME", "ACT", "body tested; roll to next expiry for net credit", {**m, **ro}))
                else:
                    acts.append(Action("STOP_LOSS", "ACT", "body tested; no credit roll available (or regime vetoed) -> exit", m))
        else:
            if abs(un.delta) < profile.untested_roll_delta - 0.05:
                pr = _propose_roll_untested(pos, snap, expiry, cfg, profile, tested_cp, width)
                if pr and pr["net_credit"] > 0:
                    acts.append(Action("ROLL_UNTESTED_IN", "ACT", f"{tested_cp} side tested; roll {pr['side']} side in to "
                                       f"{profile.untested_roll_delta:.2f}d for +{pr['net_credit']:.2f}", {**m, **pr}))
                else:
                    acts.append(Action("HOLD", "INFO", "tested but untested-side roll gives no credit; hold to stop/target", m))
            else:
                ro = _propose_roll_out(pos, snap, cfg, profile, close_cost)
                if ro and ro["net_credit"] >= 0 and not regime.vetoes:
                    acts.append(Action("ROLL_OUT_IN_TIME", "ACT", "both shorts near the money (fly-like); roll out for net credit", {**m, **ro}))
                else:
                    acts.append(Action("STOP_LOSS", "ACT", "condor already collapsed to fly-width and tested; no credit roll -> exit", m))

    if not acts:
        acts.append(Action("HOLD", "INFO", f"pnl {pnl_frac:+.0%}, dte {dte:.1f}, delta {net_delta:+.0f}", m))
    return acts
