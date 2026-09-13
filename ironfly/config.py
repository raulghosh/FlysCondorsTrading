"""All tunable thresholds live here. Defaults are common practitioner heuristics,
not recommendations; calibrate them against your own backtests."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict, fields
import json


@dataclass
class DTEProfile:
    name: str
    min_dte: int
    max_dte: int
    target_dte: int
    exit_dte: int                 # mechanical time exit (DTE); 0DTE uses exit_time instead
    no_entry_days_before_event: int
    exit_time: str = "15:30"      # ET, only used when target_dte == 0
    entry_window: tuple[str, str] = ("09:45", "15:00")  # ET; 0DTE: avoid the opening rotation
    condor_short_delta: float = 0.16
    condor_wing_width: float = 25.0     # SPX points
    fly_wing_width: float = 50.0
    condor_profit_target: float = 0.50  # fraction of entry credit
    fly_profit_target: float = 0.25
    condor_stop_mult: float = 2.0       # close when loss >= mult * credit
    fly_stop_mult: float = 1.5
    tested_delta: float = 0.30          # short strike |delta| that flags the side as tested
    untested_roll_delta: float = 0.25   # where the untested short gets rolled to
    roll_min_dte: int = 3               # below this, no adjustments, just exit rules
    max_adjustments: int = 2
    min_credit_to_width_condor: float = 0.25
    min_credit_to_width_fly: float = 0.40
    fly_recenter_frac: float = 0.50     # |spot-body| / wing as a fraction that triggers recentre


PROFILES: dict[str, DTEProfile] = {
    "0dte": DTEProfile("0dte", 0, 0, 0, 0, 0, exit_time="15:30", entry_window=("09:45", "13:30"),
                       condor_short_delta=0.10, condor_wing_width=20, fly_wing_width=30,
                       condor_profit_target=0.35, fly_profit_target=0.25,
                       condor_stop_mult=1.5, fly_stop_mult=1.0, tested_delta=0.35,
                       roll_min_dte=0, max_adjustments=1, min_credit_to_width_condor=0.08, min_credit_to_width_fly=0.30),
    "weekly": DTEProfile("weekly", 5, 9, 7, 1, 2, condor_wing_width=25, fly_wing_width=40,
                         roll_min_dte=2, max_adjustments=1),
    "monthly": DTEProfile("monthly", 30, 50, 45, 21, 1, condor_wing_width=50, fly_wing_width=75,
                          roll_min_dte=10, max_adjustments=2),
}


@dataclass
class Config:
    # account / sizing
    account_size: float = 100_000.0
    risk_per_trade_pct: float = 0.02      # max loss of one position as % of account
    max_total_risk_pct: float = 0.08      # sum of max losses across open positions
    max_positions: int = 4
    max_abs_delta_per_100k: float = 40.0  # SPX deltas (1 contract = 100 multiplier)
    vvix_size_down: float = 110.0         # halve size above this
    # regime thresholds
    ivr_lookback: int = 252
    fly_min_ivr: float = 35.0
    condor_min_ivr: float = 20.0
    min_vrp: float = 0.0                  # VIX - RV20, vol points
    max_ts_ratio: float = 1.0             # VIX / VIX3M; >1 = backwardation
    max_vvix: float = 130.0
    fly_max_adx: float = 20.0
    condor_max_adx: float = 25.0
    fly_max_sma_z: float = 0.5            # |close - SMA20| in ATR units
    condor_max_sma_z: float = 1.5
    steep_skew: float = 8.0               # 25d put IV - 25d call IV, vol points
    max_spread_pct: float = 0.10          # (ask-bid)/mid on the short strikes
    min_score: float = 0.55
    breach_adx: float = 30.0              # breached short strike + trending = close, don't roll
    # kill switch
    kill_vix_jump_pct: float = 0.20       # VIX up this much vs prior close -> no entries, defensive
    kill_daily_loss_pct: float = 0.03
    # pricing
    rate: float = 0.04
    div_yield: float = 0.013
    slippage_per_leg: float = 0.05        # points off mid, per leg
    use_oi_magnet: bool = True            # centre fly body on a high-OI strike near ATM
    # event handling
    events_file: str = "data/events.json"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Config":
        d = json.loads(text)
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})
