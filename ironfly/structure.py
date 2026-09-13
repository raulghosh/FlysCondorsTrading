"""Build and price iron condors / iron butterflies from a chain."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import date
import math
import statistics

from . import indicators as ind
from .config import Config, DTEProfile
from .feeds import Snapshot, OptionQuote
from .regime import by_delta, chain_slice


@dataclass
class Leg:
    cp: str
    strike: float
    qty: int          # -1 short, +1 long (per structure unit)
    price: float      # mid at build time
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float


@dataclass
class Structure:
    kind: str                 # IRON_FLY | IRON_CONDOR
    expiry: date
    dte: float
    legs: list[Leg]
    credit: float             # points, after slippage
    width_put: float
    width_call: float
    max_loss: float           # points (per 1 unit; x100 for dollars)
    be_lo: float
    be_hi: float
    pop: float                # P(expire between breakevens), lognormal at ATM IV
    p_touch_short: float      # ~2 x P(beyond nearer short) - crude touch probability
    delta: float
    gamma: float
    theta: float
    vega: float
    credit_to_width: float
    notes: list[str] = field(default_factory=list)

    @property
    def short_put(self) -> Leg:
        return next(l for l in self.legs if l.cp == "P" and l.qty < 0)

    @property
    def short_call(self) -> Leg:
        return next(l for l in self.legs if l.cp == "C" and l.qty < 0)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expiry"] = self.expiry.isoformat()
        return d


def _nearest(chain, expiry, cp, strike) -> OptionQuote | None:
    s = [q for q in chain_slice(chain, expiry, cp) if q.mid > 0]
    return min(s, key=lambda q: abs(q.strike - strike)) if s else None


def _leg(q: OptionQuote, qty: int) -> Leg:
    return Leg(q.cp, q.strike, qty, q.mid, q.iv, q.delta, q.gamma, q.theta, q.vega)


def assemble(kind: str, snap: Snapshot, expiry: date, sp: OptionQuote, lp: OptionQuote, sc: OptionQuote,
             lc: OptionQuote, cfg: Config, notes: list[str] | None = None) -> Structure:
    legs = [_leg(sp, -1), _leg(lp, 1), _leg(sc, -1), _leg(lc, 1)]
    credit = sum(-l.qty * l.price for l in legs) - cfg.slippage_per_leg * 4
    wp, wc = sp.strike - lp.strike, lc.strike - sc.strike
    max_loss = max(wp, wc) - credit
    be_lo, be_hi = sp.strike - credit, sc.strike + credit
    T = snap.dte(expiry) / 365
    atm_iv = statistics.mean([sp.iv, sc.iv]) / 100 if sp.iv and sc.iv else 0.2
    pop = ind.prob_between(snap.spot, be_lo, be_hi, atm_iv, T, cfg.rate, cfg.div_yield)
    p_out = 1 - ind.prob_between(snap.spot, sp.strike, sc.strike, atm_iv, T, cfg.rate, cfg.div_yield)
    g = lambda k: sum(l.qty * getattr(l, k) for l in legs)
    width = max(wp, wc)
    return Structure(kind, expiry, round(snap.dte(expiry), 3), legs, round(credit, 2), wp, wc, round(max_loss, 2),
                     round(be_lo, 2), round(be_hi, 2), round(pop, 3), round(min(1.0, 2 * p_out), 3),
                     round(g("delta"), 4), round(g("gamma"), 6), round(g("theta"), 3), round(g("vega"), 3),
                     round(credit / width, 3) if width else 0.0, notes or [])


def build_condor(snap: Snapshot, expiry: date, cfg: Config, profile: DTEProfile,
                 short_delta: float | None = None, width: float | None = None) -> Structure | None:
    sd, w = short_delta or profile.condor_short_delta, width or profile.condor_wing_width
    sp = by_delta(snap.chain, expiry, "P", sd)
    sc = by_delta(snap.chain, expiry, "C", sd)
    if not (sp and sc) or sp.strike >= sc.strike:
        return None
    lp, lc = _nearest(snap.chain, expiry, "P", sp.strike - w), _nearest(snap.chain, expiry, "C", sc.strike + w)
    if not (lp and lc) or lp.strike >= sp.strike or lc.strike <= sc.strike:
        return None
    s = assemble("IRON_CONDOR", snap, expiry, sp, lp, sc, lc, cfg)
    if s.credit_to_width < profile.min_credit_to_width_condor:
        s.notes.append(f"credit/width {s.credit_to_width:.2f} < min {profile.min_credit_to_width_condor}: premium too thin")
    return s


def magnet_strike(snap: Snapshot, expiry: date, cfg: Config) -> float | None:
    """High open-interest strike within 0.5% of spot (pin candidate)."""
    if not cfg.use_oi_magnet:
        return None
    near = [q for q in snap.chain if q.expiry == expiry and abs(q.strike - snap.spot) / snap.spot < 0.005]
    if not near or all(q.oi == 0 for q in near):
        return None
    oi = {}
    for q in near:
        oi[q.strike] = oi.get(q.strike, 0) + q.oi
    all_oi = [q.oi for q in snap.chain if q.expiry == expiry and q.oi > 0]
    k, v = max(oi.items(), key=lambda kv: kv[1])
    return k if all_oi and v >= 2 * statistics.median(all_oi) else None


def build_fly(snap: Snapshot, expiry: date, cfg: Config, profile: DTEProfile,
              body: float | None = None, width: float | None = None) -> Structure | None:
    w = width or profile.fly_wing_width
    notes = []
    if body is None:
        body = magnet_strike(snap, expiry, cfg)
        if body:
            notes.append(f"body on OI magnet {body:.0f}")
    sp = _nearest(snap.chain, expiry, "P", body or snap.spot)
    if not sp:
        return None
    sc = _nearest(snap.chain, expiry, "C", sp.strike)
    lp, lc = _nearest(snap.chain, expiry, "P", sp.strike - w), _nearest(snap.chain, expiry, "C", sp.strike + w)
    if not (sc and lp and lc) or sc.strike != sp.strike or lp.strike >= sp.strike or lc.strike <= sc.strike:
        return None
    s = assemble("IRON_FLY", snap, expiry, sp, lp, sc, lc, cfg, notes)
    if s.credit_to_width < profile.min_credit_to_width_fly:
        s.notes.append(f"credit/width {s.credit_to_width:.2f} < min {profile.min_credit_to_width_fly}: premium too thin")
    return s


def reprice(legs: list[dict], snap: Snapshot, expiry: date, cfg: Config) -> tuple[float, list[Leg]]:
    """Current cost to close (points, positive = debit) and refreshed legs with live greeks."""
    out, cost = [], 0.0
    for l in legs:
        q = _nearest(snap.chain, expiry, l["cp"], l["strike"])
        if q is None or q.strike != l["strike"]:
            raise ValueError(f"strike {l['strike']}{l['cp']} {expiry} not in chain")
        out.append(_leg(q, l["qty"]))
        cost += -l["qty"] * q.mid          # buying back shorts costs, selling longs pays
    return cost + cfg.slippage_per_leg * len(legs), out
