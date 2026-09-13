"""Position sizing and portfolio-level limits."""
from __future__ import annotations
import math
from .config import Config
from .regime import Regime
from .structure import Structure


def size(s: Structure, regime: Regime, cfg: Config, open_risk_usd: float, open_delta: float, n_open: int) -> tuple[int, list[str]]:
    reasons = []
    per = s.max_loss * 100
    if per <= 0:
        return 0, ["max loss <= 0 (bad prices)"]
    budget = cfg.account_size * cfg.risk_per_trade_pct
    n = math.floor(budget / per)
    reasons.append(f"risk budget ${budget:,.0f} / max loss ${per:,.0f} -> {n}")
    conf = max(regime.fly_score if s.kind == "IRON_FLY" else regime.condor_score, 0)
    if conf < 0.75:
        n = int(n * 0.75 + 0.5); reasons.append(f"score {conf:.2f} < 0.75 -> x0.75")
    if regime.vvix and regime.vvix > cfg.vvix_size_down:
        n = math.floor(n / 2); reasons.append(f"VVIX {regime.vvix:.0f} > {cfg.vvix_size_down} -> x0.5")
    if regime.ivr > 80:
        n = math.floor(n / 2); reasons.append("IVR > 80 (vol can keep expanding) -> x0.5")
    room = cfg.account_size * cfg.max_total_risk_pct - open_risk_usd
    if room < per * max(n, 1):
        n = max(0, math.floor(room / per)); reasons.append(f"portfolio risk room ${room:,.0f} -> {n}")
    if n_open >= cfg.max_positions:
        n = 0; reasons.append(f"max positions {cfg.max_positions} reached")
    delta_cap = cfg.max_abs_delta_per_100k * cfg.account_size / 100_000
    if abs(open_delta + s.delta * 100 * n) > delta_cap:
        n = 0; reasons.append(f"portfolio delta would exceed ±{delta_cap:.0f}")
    return max(n, 0), reasons


def kill_switch(regime: Regime, daily_pnl_usd: float, cfg: Config) -> list[str]:
    out = []
    if regime.vix_jump >= cfg.kill_vix_jump_pct:
        out.append(f"VIX +{regime.vix_jump:.0%} on the day: no new entries, defend open positions")
    if daily_pnl_usd <= -cfg.kill_daily_loss_pct * cfg.account_size:
        out.append(f"daily loss ${daily_pnl_usd:,.0f} beyond {cfg.kill_daily_loss_pct:.0%}: flatten, stop trading today")
    return out
