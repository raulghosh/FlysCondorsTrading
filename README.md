# ironfly — SPX iron butterfly / iron condor signal engine

Signals only. It tells you **when the environment supports a short-premium, defined-risk SPX
structure, which one, how big, and what to do with it afterwards**. It never sends an order.
Thresholds are practitioner heuristics, not advice; calibrate them on your own data.

```bash
python -m ironfly demo                       # synthetic data, end to end
python -m ironfly scan --profile weekly      # entry scan (synthetic by default)
python -m ironfly scan --feed schwab --profile monthly
python -m ironfly paper-open --feed schwab   # record last entry signal into positions.json
python -m ironfly manage --feed schwab       # actions for every position in positions.json
python -m ironfly show-config > config.json  # then edit; every threshold lives there
python tests/test_core.py
```

Data: `SCHWAB_ACCESS_TOKEN` (SPX chain + $VIX family), or `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`
(Alpaca has no SPX index options, so it runs on SPY x10 and pulls VIX/VIX3M/VVIX/VIX9D from CBOE's
free CSVs). Any other broker: implement `MarketFeed.snapshot()` in `ironfly/feeds.py`, or dump
`history.csv` + `chain.csv` and use `--feed csv`.

---

## 1. The thesis, in one paragraph

Both structures sell the variance risk premium (implied vol > subsequently realized vol) inside a
range. They make money when the index **realizes less than it implied** and lose when it
**trends or gaps**. So the whole system asks three questions, in order:

1. **Is premium rich enough and is the vol regime stable?** (IV rank, VRP, term structure, VVIX)
2. **Is price behaving like a range, not a trend?** (ADX, distance from mean, realized-vol compression)
3. **Is anything scheduled that breaks 1 or 2?** (FOMC, CPI, NFP, opex)

Fly vs condor is then a question of *how confident you are in the pin*: the fly is the
high-credit, narrow-zone bet on mean reversion; the condor is the lower-credit, wide-zone bet on
"not much happens".

## 2. Environment signals (`ironfly/regime.py`)

| Signal | What it measures | Fly wants | Condor wants | Veto |
|---|---|---|---|---|
| IV rank / percentile (VIX, 252d) | Is premium rich vs its own history | ≥ 35 | ≥ 20 | < 20 |
| VRP = VIX − RV20 (close-close; Yang-Zhang shown too) | Is implied > realized | > 0, bigger better | same | < 0 |
| VIX / VIX3M | Term structure | < 1 (contango) | < 1 | > 1 backwardation |
| VIX9D / VIX | Front-end stress | < 1 | < 1 | warning only |
| VVIX | Vol of vol | < 110 | < 110 | > 130 |
| ADX(14) | Trend strength | < 20 | < 25 | > 25 |
| (close − SMA20) / ATR | Extension from mean | |z| < 0.5 | |z| < 1.5 | > 1.5 |
| RV5 / RV20 | Realized vol compressing | < 1 | any | rising = warning |
| 25Δ put IV − 25Δ call IV | Skew | — | — | > 8 vol pts = warning |
| Bid/ask on the 16Δ strikes | Liquidity | < 10 % of mid | same | > 10 % |
| VIX vs prior close | Vol shock | — | — | > +20 % kill switch |
| Event calendar | FOMC/CPI/NFP inside the no-entry window | none | none | any |

Each strategy gets a 0–1 score from the first five rows (weights 25/25/20/20/10). Any veto → `NONE`.
Fly only if it also meets its own hard limits (IVR ≥ 35, ADX ≤ 20, |z| ≤ 0.5); otherwise condor if
its score ≥ `min_score` (0.55). The fly body snaps to a high-open-interest strike within 0.5 % of
spot when one exists (pin magnet), otherwise ATM.

## 3. DTE profiles (`ironfly/config.py`)

| | 0dte | weekly | monthly |
|---|---|---|---|
| Target DTE | 0 (enter 09:45–13:30 ET) | 7 (5–9) | 45 (30–50) |
| Condor short delta / wing | 10Δ / 20 pts | 16Δ / 25 | 16Δ / 50 |
| Fly wing | 30 | 40 | 75 |
| Min credit/width | 0.08 condor · 0.30 fly | 0.25 · 0.40 | 0.25 · 0.40 |
| Profit target (of credit) | 35 % condor · 25 % fly | 50 % · 25 % | 50 % · 25 % |
| Stop (multiple of credit) | 1.5× · 1.0× | 2.0× · 1.5× | 2.0× · 1.5× |
| Time exit | 15:30 ET | 1 DTE | 21 DTE |
| No entry if event within | same day | 2 days | 1 day |
| Adjustments allowed | 1 (none < 0 DTE) | 1 (none < 2 DTE) | 2 (none < 10 DTE) |

## 4. Sizing and portfolio limits (`ironfly/risk.py`)

- Contracts = floor(account × 2 % / max loss per contract).
- × 0.75 if regime score < 0.75; × 0.5 if VVIX > 110; × 0.5 if IVR > 80 (vol can keep expanding).
- Total open max-loss ≤ 8 % of account; ≤ 4 positions; |portfolio delta| ≤ 40 per $100k.
- Kill switch: VIX +20 % on the day or daily loss ≥ 3 % → no entries, defend what is open.

## 5. Holding period: the trade's own half-life

There is no universal "hold for N days". Each structure gets a **decay curve** at entry: reprice
the four legs with spot and IV frozen and only time passing (`structure.decay_curve`). From it:

| Number | Meaning | Typical (synthetic, 16Δ condor) |
|---|---|---|
| `half_life_days` | when the premium is expected to have halved | 0DTE ≈ 3 h · weekly ≈ 3.5 d · 45-DTE ≈ 22 d |
| `days_to_target` | when the profile's profit target should be hit | same as half-life for a 50 % target; earlier for the fly's 25 % |
| `planned_hold_days` | entry → mechanical time exit (15:30 for 0DTE, 1 DTE weekly, 21 DTE monthly) | 0.2 d · 6 d · 24 d |

The 45-DTE half-life landing at ~22 days is why "manage at 21 DTE" works: past that point most of
the remaining credit is gamma risk, not theta. If `days_to_target` falls outside `planned_hold_days`
the structure is flagged at scan time ("premium too thin for this DTE") and is not an entry.

While the position is open, `Position.schedule()` recomputes where the trade *should* be on that
curve today and `manage` compares it with actual P&L:

| Rule | Trigger | Action |
|---|---|---|
| Ahead of schedule | ≥ 75 % of target captured within 40 % of `days_to_target` | TAKE_PROFIT now — the remaining edge is small relative to the gamma you keep holding |
| Stale | held ≥ 1.5 × `days_to_target` and target not reached | STALE_EXIT — theta is not paying; capital is better redeployed into a fresh structure |
| Lagging | past half the planned hold and P&L < 50 % of the expected curve | LAGGING flag — spot or IV moved against you; never add, take the first exit that triggers |

Positions entered by hand can still use these rules if `positions.json` carries each leg's entry
`iv` and `price` plus `entry_spot`; `paper-open` records them automatically.

**So, in order, a trade's life is:** enter → let theta run to the target (~half-life) → if it gets
there early, take it → if it stalls past 1.5× the expected time, leave → never hold past the time
exit regardless of P&L → and at any point a tested strike triggers the adjustment ladder in §6.

## 6. Management playbook (`ironfly/manage.py`)

Evaluated every run for every position, in this order (first hit wins for the hard exits).
The schedule rules from §5 run between the hard exits (1–3) and the environment rules (4).

1. **TAKE_PROFIT** at the profile target. Flies are closed at 25 % of credit; they almost never
   reach max profit and the last 75 % is where the gamma lives.
2. **STOP_LOSS** at the stop multiple, or immediately if a short strike is breached while ADX ≥ 30
   (trending market: do not roll into a trend).
3. **TIME_EXIT** at the profile's DTE (21 for 45-DTE entries, 1 for weeklies) or at 15:30 ET for 0DTE.
4. **REGIME_EXIT / TIGHTEN_STOP** when the environment turns hostile (backwardation, VVIX > 130,
   vol shock): book any gain now; if under water, stop halves to 1× credit.
5. **EVENT_EXIT** when FOMC/CPI/NFP lands inside the last two days of a weekly/monthly.
6. **Tested side** (short strike |Δ| ≥ 0.30, or 0.35 for 0DTE), only if adjustments remain and DTE > roll floor:
   - Condor, untested short still far OTM → **ROLL_UNTESTED_IN** to 25Δ for a net credit
     (reduces delta, adds credit, widens the loss-side buffer). Proposal includes exact legs and net credit.
   - Condor already squeezed to fly width, or a fly with body tested → **ROLL_OUT_IN_TIME** to the
     next expiry in profile, same structure rebuilt at current deltas, **only for net credit and only
     if the regime still passes**. Otherwise STOP_LOSS.
   - Fly drifted ≥ 50 % of wing from body while regime still says fly → **RECENTER_FLY** (close, re-open ATM).
   - Adjustments exhausted → STOP_LOSS. Never a third roll; that is how small losses become max losses.
7. Otherwise **HOLD** with live P&L, deltas, and DTE.

Rules that never bend: roll only for a credit, never roll into an event window, never roll into
backwardation, never add contracts to a losing position.

## 7. Structure maths (`ironfly/structure.py`, `ironfly/indicators.py`)

Black-Scholes European with continuous dividend (SPX is cash-settled European; r = 4 %, q = 1.3 %,
both in config). Broker greeks are used when supplied; otherwise IV is solved from mid and greeks
computed. Credit is mid less 0.05/leg slippage; close cost adds the same. POP is the lognormal
probability of finishing between breakevens at the shorts' average IV; P(touch) is the usual 2 ×
P(finish beyond a short). Expiry time is 16:00 ET (PM-settled weeklies; the AM-settled monthly is
6.5 h earlier, immaterial above 1 DTE).

## 8. Files

```
ironfly/config.py      thresholds, DTE profiles, JSON config
ironfly/feeds.py       Snapshot/OptionQuote; CSV, Schwab, Alpaca, Synthetic feeds; CBOE index history
ironfly/indicators.py  BSM, IV solver, realized vol (CC, Yang-Zhang), IV rank, ADX/ATR, expected move
ironfly/events.py      FOMC/CPI/NFP/PCE 2026 seed + data/events.json + computed opex/quad-witching
ironfly/regime.py      classify() -> Regime(strategy, scores, vetoes, warnings)
ironfly/structure.py   build_condor / build_fly / reprice / OI magnet
ironfly/manage.py      evaluate(position) -> [Action]
ironfly/risk.py        size() and kill_switch()
ironfly/engine.py      Engine.scan() / Engine.manage(); appends to signals.jsonl
ironfly/__main__.py    CLI
tests/test_core.py     self-check
```

`positions.json` is a plain list you (or `paper-open`) maintain; the engine never assumes a fill.
Every scan and every management decision is appended to `signals.jsonl` for later review.

## 9. What to add when you have the data

- **GEX / dealer gamma** (needs full OI by strike): positive GEX supports the fly, negative argues
  for wider condors or nothing. Hook: `structure.magnet_strike` is the natural home.
- **Intraday 0DTE features**: opening-range width vs expected move, VWAP distance, 1-minute realized vol.
- **A backtester** over stored snapshots: the `CSVFeed` + `ts` argument already allow replay.
- **Event calendar upkeep**: FOMC, CPI, the jobs report, and PCE are seeded for all of 2026 in
  `ironfly/events.py` from the Fed/BLS/BEA official calendars. Re-pull and update `FOMC_2026` /
  `CPI_2026` / `NFP_2026` / `PCE_2026` each year (roughly Q4, once the next year's calendars are
  published) or as revisions land. Anything else — an unseeded print, an earnings date, a
  geopolitical event — goes in `data/events.json` (`[{"date":"2026-10-14","name":"CPI"}]`), which
  is merged on top of the seed.
