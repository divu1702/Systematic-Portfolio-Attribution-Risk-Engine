# Roadmap

Status as of 8 Nov 2026. Items are in priority order within each group. Threshold changes still follow
the tuning rule (in batches, recorded in CHANGELOG.md); backtest variants below are diagnostics, not
new defaults.

## What the real-data backtest showed

Two years of real end-of-day US option quotes (DoltHub, 48 liquid stocks, Oct 2024 to Oct 2026)
replayed through the live code with the current rules produced **no trades**. The funnel over
29,981 stock-days:

| Stage | Removed | Note |
|---|---|---|
| F2 events | 11,302 | earnings inside the holding window, incl. estimated dates |
| F3 IV/HV >= 1.25 | 14,914 | the core "IV is rich" test |
| F4 IV percentile, F5 cause passed | 3,543 | 219 stock-days (0.7%) left with a valid setup |
| direction, F7 | 62 | |
| F9 liquidity | 145 | end-of-day quotes: median bid-ask 14% of mid on short legs, 30% on long legs |
| F8 credit/width >= 25% | 12 | best spread reached 24.9% |

Single-threshold variants (bid-ask 10%, IV/HV 1.10, credit/width 20%) also produced no trades.

**The structural finding:** F8 (credit >= 25% of width) and F7 (short delta 0.20-0.30) nearly exclude
each other. A vertical spread's credit as a share of its width is bounded by roughly the probability
that the short strike finishes in the money, which is close to its delta, minus what crossing the
bid-ask costs. At mid prices with no costs, 25% needs a ~0.30-delta short, a 1-2 strike width and
elevated IV; at natural prices almost nothing qualifies. This holds in any market, NSE included.
The path-simulated EV filter already tests whether the credit pays for the risk, which is what F8
is meant to ensure.

### Research variants (diagnostics, not new defaults)

| Variant | Trades | Win rate (breakeven) | Net on $100k | Profit factor | Max DD |
|---|---|---|---|---|---|
| A: credit/width 15%, bid-ask 20% | 0 | - | - | - | - |
| B: A + IV/HV 1.10 | 0 | - | - | - | - |
| C: A + spec's expiry-based EV | 19 | 37% (82%) | -$11,114 | 0.25 | 11.6% |
| D: C without the thesis stop | 12 | 42% (81%) | -$10,590 | 0.20 | 10.7% |

- In A and B the path-simulated EV rejected every candidate. Decomposing it on those spreads (share
  of max loss): +3.1% with no exit rules, +2.9% with the hard stop only, +1.4% with the 50% profit
  target only, +0.7% with all rules; crossing the end-of-day bid-ask twice costs about 6%. The
  edge (IV above forecast vol) is real but small, and the profit target removes about half of it.
- In C, where trades were allowed, the expiry-based p_model said 71-80% and 37% of trades won.
  Both halves of the period lost money. Bear calls (7 trades, 1 winner) lost most in a rising market.
- Hard stops lost $1.3-2.3k each against $0.4-0.7k per winner. Most were narrow spreads with tiny
  credits (0.17-0.41 a share): the 2x-credit stop on natural prices fired on bid-ask noise, and sizing
  by risk at the stop made those positions large (29-60 contracts). Losses ran 1.6-2x the planned risk
  because stops were checked once a day.
- Caveats: 19 trades is a small sample; US market; end-of-day quotes are wider than intraday quotes and
  stops were checked at the close. The direction of the evidence is still consistent across halves.

## Decisions for the owner

1. **F8.** *Done 8 Nov 2026:* replaced by a friction test (credit at least 3x the bid-ask cost of
   closing, at least 5 ticks; `spread.f8_mode`), with the spec's 25% rule one setting away.
2. **Which strategy fixes to test next** (each as a config option, judged on the first half of the
   data and confirmed on the second half, per the tuning rule):
   - *Minimum credit versus the bid-ask (done: F8 friction mode, plus `risk.max_lots_per_trade`):*
     require the net credit to be at least 2x the two legs'
     combined bid-ask, plus a minimum in ticks, and cap contracts per trade. Removes noise-driven stops
     and oversized positions on tiny credits.
   - *Stops on the mid price (done: `exit.stop_price: mid`, the default):* trigger the hard stop on the mid-price cost to close and fill at natural
     prices when it fires, so a wide quote alone cannot stop a trade out.
   - *Profit target:* 50% halves the modelled edge; test 65-75% or holding to the time stop.
   - *A forward-looking volatility forecast for F3:* after a large move realised volatility tends to
     stay high, so IV / HV20 overstates how rich IV is. Compare IV with an EWMA or GARCH forecast, or
     require the last 5 days' realised volatility to have dropped back ("cause has passed").
   - *Trend filter for bear calls:* only sell call spreads when the stock is below, say, its 50-day
     average.
3. **Event source for NSE.** NSE refuses honest automated clients. Either drop the Board Meetings and
   Corporate Actions CSV exports from nseindia.com into `imports/events/` about weekly (free), or
   buy a licensed corporate-actions feed (paid). Without one of these, F2 fails closed and nothing trades.

## Next improvements

### Evidence and research
- **Start paper trading early, even before trades are expected.** The live recorder builds an NSE
  history with real bid/ask and depth from day one; after about three months it can be replayed with
  `backtest run --market nse`.
- **Walk-forward discipline.** Choose any threshold change on the first part of a history and confirm
  it on the later part (backtest date ranges and `--set` support this).
- **Model IV mean reversion in the EV simulation.** IV is held at its entry level today, which ignores
  the fall the strategy is betting on. Calibrate a reversion speed from recorded IV snapshots.
- **Better realised volatility.** A Yang-Zhang estimator (uses open/high/low/close) is less noisy than
  close-to-close HV20, which matters because F3 compares IV against it. Compare as a backtest variant.

### Risk
- **Portfolio Greeks.** Cap net delta and vega across open spreads, on top of the count, direction and
  sector limits, so several spreads cannot add up to one large directional bet.
- **Market-wide events.** Optional market-wide event dates (RBI policy, budget, election results) in
  events_override.csv with a symbol of `*`.

### Operations
- **Run as a supervised service** (Windows Task Scheduler or systemd) with automatic restart; the
  end-of-day heartbeat alert shows when it is down.
- **Cloud VM in Mumbai** when moving towards live trading (needed for the static IP anyway).

### Live trading (milestone 8)
- Order router with limit stepping, leg sequencing, reconciliation and kill switches, once paper
  trading clears the go-live bars.

## Done on 8 Nov 2026
- Real-data backtest: DoltHub loader, historical broker, end-of-day replay through the live code,
  `--set` variants, stability split, HTML report.
- Live recorder for NSE quotes and `backtest run --market nse`.
- Batched scan quotes (about a minute for ~220 stocks instead of 7-15 minutes).
- Honest NSE client with browser-export event imports.
- Config hash on every candidate and trade; daily database backup; end-of-day heartbeat.
- Market profiles (currency) and a per-contract cost model.
- Market-independent fixes from the research review: F8 friction test, mid-price hard stop, optional
  lot cap (all config options).
- Kite history archiver: nightly saving of listed option and futures candles before expiry, plus a
  one-off backfill (`python -m app archive`).
- Research study ported to Indian data (`--market nse`), ready to run as the archive grows.
