"""Environment classification: is this a market where short-premium, defined-risk SPX
structures have edge right now, and which one (fly vs condor)?"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import date

import numpy as np

from . import indicators as ind
from .config import Config, DTEProfile
from .events import blocking_events
from .feeds import Snapshot, OptionQuote


@dataclass
class Regime:
    strategy: str                      # IRON_FLY | IRON_CONDOR | NONE
    fly_score: float
    condor_score: float
    ivr: float
    ivp: float
    vix: float
    rv20: float
    rv5: float
    yz20: float
    vrp: float                         # vix - rv20 (vol points)
    ts_ratio: float | None             # VIX / VIX3M
    ts9_ratio: float | None            # VIX9D / VIX
    vvix: float | None
    adx: float
    sma_z: float
    bb_width_pct: float
    skew25: float | None
    atm_iv: float | None
    expected_move: float | None
    spread_pct: float | None
    vix_jump: float                    # vix / prev close - 1
    vetoes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def hostile(self) -> bool:
        """True when open positions should be defended, not just no new entries."""
        return any(v in self.vetoes for v in ("BACKWARDATION", "VVIX_EXTREME", "VOL_SHOCK"))

    def to_dict(self) -> dict:
        return asdict(self)


def _ramp(x, lo, hi) -> float:
    """0 at lo, 1 at hi (or reversed if lo > hi)."""
    if lo == hi:
        return 1.0
    return float(np.clip((x - lo) / (hi - lo), 0, 1))


def pick_expiry(snap: Snapshot, profile: DTEProfile) -> date | None:
    cands = [(abs(snap.dte(e) - profile.target_dte), e) for e in snap.expiries()
             if profile.min_dte <= round(snap.dte(e)) <= profile.max_dte]
    return min(cands)[1] if cands else None


def chain_slice(chain: list[OptionQuote], expiry: date, cp: str) -> list[OptionQuote]:
    return sorted((q for q in chain if q.expiry == expiry and q.cp == cp), key=lambda q: q.strike)


def by_delta(chain: list[OptionQuote], expiry: date, cp: str, target: float) -> OptionQuote | None:
    s = [q for q in chain_slice(chain, expiry, cp) if q.mid > 0]
    return min(s, key=lambda q: abs(abs(q.delta) - target)) if s else None


def skew_and_atm(snap: Snapshot, expiry: date) -> tuple[float | None, float | None, float | None]:
    p, c = by_delta(snap.chain, expiry, "P", 0.25), by_delta(snap.chain, expiry, "C", 0.25)
    atm_p, atm_c = by_delta(snap.chain, expiry, "P", 0.50), by_delta(snap.chain, expiry, "C", 0.50)
    skew = (p.iv - c.iv) if p and c else None
    atm = ((atm_p.iv + atm_c.iv) / 2) if atm_p and atm_c else None
    sp = max(p.spread_pct, c.spread_pct) if p and c else None
    return skew, atm, sp


def classify(snap: Snapshot, cfg: Config, profile: DTEProfile, events: list[dict]) -> Regime:
    h = snap.history
    rv20 = float(ind.realized_vol_cc(h["close"], 20).iloc[-1])
    rv5 = float(ind.realized_vol_cc(h["close"], 5).iloc[-1])
    yz20 = float(ind.yang_zhang(h, 20).iloc[-1]) if {"open", "high", "low"} <= set(h.columns) else rv20
    ivr, ivp = ind.iv_rank(h["vix"], cfg.ivr_lookback)
    vrp = snap.vix - rv20
    ts = snap.vix / snap.vix3m if snap.vix3m else None
    ts9 = snap.vix9d / snap.vix if snap.vix9d else None
    adx = float(ind.adx(h).iloc[-1])
    z = ind.sma_zscore(h)
    bbw = ind.bollinger_width_pct(h["close"])
    vix_jump = snap.vix / snap.vix_prev_close - 1 if snap.vix_prev_close else 0.0

    expiry = pick_expiry(snap, profile)
    skew, atm_iv, spread = skew_and_atm(snap, expiry) if expiry else (None, None, None)
    em = ind.expected_move(snap.spot, atm_iv, snap.dte(expiry)) if (expiry and atm_iv) else None

    vetoes, warns = [], []
    if expiry is None:
        vetoes.append("NO_EXPIRY_IN_PROFILE")
    if ts is not None and ts > cfg.max_ts_ratio:
        vetoes.append("BACKWARDATION")
    if vrp < cfg.min_vrp:
        vetoes.append("NEGATIVE_VRP")
    if snap.vvix is not None and snap.vvix > cfg.max_vvix:
        vetoes.append("VVIX_EXTREME")
    if vix_jump > cfg.kill_vix_jump_pct:
        vetoes.append("VOL_SHOCK")
    if ivr < cfg.condor_min_ivr:
        vetoes.append("IV_RANK_LOW")
    if adx > cfg.condor_max_adx:
        vetoes.append("TRENDING")
    if abs(z) > cfg.condor_max_sma_z:
        vetoes.append("EXTENDED_FROM_MEAN")
    if spread is not None and spread > cfg.max_spread_pct:
        vetoes.append("ILLIQUID")
    ev = blocking_events(snap.ts.date(), profile.no_entry_days_before_event, events)
    ev_names = [f"{e['name']}@{e['date']}" for e in ev]
    if ev:
        vetoes.append("EVENT_" + ev[0]["name"].replace("(approx)", ""))
    hhmm = snap.ts.strftime("%H:%M")
    if profile.target_dte == 0 and not (profile.entry_window[0] <= hhmm <= profile.entry_window[1]):
        vetoes.append("OUTSIDE_ENTRY_WINDOW")

    if skew is not None and skew > cfg.steep_skew:
        warns.append(f"STEEP_PUT_SKEW({skew:.1f})")
    if snap.vvix is not None and snap.vvix > cfg.vvix_size_down:
        warns.append(f"VVIX_ELEVATED({snap.vvix:.0f})")
    if ts9 is not None and ts9 > 1.0:
        warns.append("FRONT_END_INVERTED(9d>30d)")
    if rv5 > rv20 * 1.3:
        warns.append("REALIZED_VOL_RISING")
    if ivr > 80:
        warns.append("IV_RANK_EXTREME(consider smaller size, wider wings)")

    # scores (0..1). Fly wants tighter conditions than condor.
    compress = _ramp(rv5 / max(rv20, 1e-6), 1.2, 0.6)
    vrp_s = _ramp(vrp, 0, 6)
    fly = (0.25 * _ramp(ivr, cfg.fly_min_ivr - 10, cfg.fly_min_ivr + 15)
           + 0.25 * vrp_s
           + 0.20 * _ramp(adx, cfg.fly_max_adx + 8, cfg.fly_max_adx - 5)
           + 0.20 * _ramp(abs(z), cfg.fly_max_sma_z + 0.5, cfg.fly_max_sma_z - 0.3)
           + 0.10 * compress)
    condor = (0.25 * _ramp(ivr, cfg.condor_min_ivr - 5, cfg.condor_min_ivr + 20)
              + 0.25 * vrp_s
              + 0.20 * _ramp(adx, cfg.condor_max_adx + 8, cfg.condor_max_adx - 8)
              + 0.20 * _ramp(abs(z), cfg.condor_max_sma_z + 0.5, cfg.condor_max_sma_z - 0.7)
              + 0.10 * (0.5 + 0.5 * compress))
    if ts is not None:
        fly *= _ramp(ts, 1.0, 0.85) * 0.3 + 0.7
        condor *= _ramp(ts, 1.0, 0.85) * 0.3 + 0.7

    strategy = "NONE"
    if not vetoes:
        if fly >= cfg.min_score and fly >= condor and ivr >= cfg.fly_min_ivr and adx <= cfg.fly_max_adx and abs(z) <= cfg.fly_max_sma_z:
            strategy = "IRON_FLY"
        elif condor >= cfg.min_score:
            strategy = "IRON_CONDOR"
    return Regime(strategy, round(fly, 3), round(condor, 3), round(ivr, 1), round(ivp, 1), snap.vix, round(rv20, 2),
                  round(rv5, 2), round(yz20, 2), round(vrp, 2), round(ts, 3) if ts else None,
                  round(ts9, 3) if ts9 else None, snap.vvix, round(adx, 1), round(z, 2), round(bbw, 1),
                  round(skew, 2) if skew is not None else None, round(atm_iv, 2) if atm_iv else None,
                  round(em, 1) if em else None, round(spread, 3) if spread is not None else None,
                  round(vix_jump, 4), vetoes, warns, ev_names)
