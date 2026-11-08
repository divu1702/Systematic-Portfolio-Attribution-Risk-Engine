# Research plan: what we will test on Indian data, written down before we see it

8 Nov 2026. These hypotheses come from the literature, the US study ([strategy_review.md](strategy_review.md))
and the owner's ideas (high-beta premium, retail demand). They are fixed now, before any Indian data
is collected, so that the data tests them instead of suggesting them. Results go into this folder as
they come in, including the ones that fail.

## Rules

- **One test per hypothesis**, on data not used to choose its thresholds. Every result is reported.
- **An effect counts** only if it has the same sign in both halves of the period and |t| >= 2.5
  overall (Newey-West, across dates). With about twenty hypotheses, one would pass |t| >= 2 by chance.
- **Thresholds** may be chosen on the first half and confirmed on the second; never chosen on both.
- **US results are priors**, not evidence about India.
- **A strategy change** needs its hypothesis to pass and a paper-trading period that agrees.

## Stock prices only: runnable on the laptop's first night

`python research/india_stock_study.py > research/results_india_stocks.md` uses the daily bars the
archiver saves on its first run (about 5 years for every F&O stock, plus the NIFTY 50). Strikes are
placed at 0.25 delta from a volatility forecast known at entry; holding window 15 trading days.

| # | Hypothesis | Measure | If it holds |
|---|---|---|---|
| D1 | Stocks above their 50-day average hit put strikes less and call strikes more than stocks below | Tilt (call hits minus put hits, against all stocks on the same dates) | Choose the spread side by trend, not by support/resistance |
| D2 | The top third by 3- or 6-month return (skipping the last week) has positive tilt; the bottom third negative | Same | Use momentum with, or instead of, trend |
| D3 | Last week's biggest movers partly reverse | Tilt of last week's terciles | Avoid entries that fight a sharp one-week move |
| D4 | After a large move (1.5 ATR in 10 days) volatility stays above its forecast | Later volatility / forecast, by shock | Confirms replacing F5 with "the shock has passed" |
| D5 | Our support/resistance levels are reached as often as same-size moves on any day (no information) | Level test, as in the US study | Drop levels from direction and strike placement (F6) |
| V1 | HAR or EWMA forecasts the next 3 weeks' volatility better than HV20 | Median absolute log error; rank correlation across stocks (2nd half, out of sample) | Use the best as the denominator of IV richness and in the path EV |
| B1 | High-beta stocks' put strikes are hit together on the index's worst windows | Put hits on the worst 10% of index windows, by beta third | Cap the book's beta-weighted delta and the number of open high-beta put spreads |

## Needs option history: from the archive

First answers after about 6-8 completed monthly expiries, firmer after about 24. Run with
`python research/option_selling_study.py --market nse`, which prices each option at its trade nearest
15:00 against the futures at the same minute.

| # | Hypothesis | Measure | If it holds |
|---|---|---|---|
| R1 | IV percentile >= 70 (F4) means richer premium and better short-option returns (US prior: yes) | IV / later realised vol; P&L held to expiry | Keep F4 as the main richness test |
| R2 | Ranking stocks by IV / forecast volatility finds richer premium than IV percentile alone | Premium of the top fifth by each ranking | Replace F3 with the ranking |
| R3 | High-beta or high stock-specific-volatility stocks carry more premium per unit of risk, after costs (owner's idea; Cao and Han found it for stock-specific volatility in the US) | IV / later realised vol and net P&L per unit of max loss, by beta third | Favour them, within B1's cap |
| R4 | Wings of about 5-6% of the stock price beat 1-strike wings at natural prices (US prior: yes) | Net P&L / width | Choose the wing by % of spot |
| R5 | A 75% profit target with mid-price stops beats a 50% target with natural-price stops | Intraday replay at the 5-minute marks | Change the exit rules |
| R6 | When IV is cheap and the trend strong, a debit spread in the trend's direction has positive expected value after costs | Net P&L | Add debit spreads to the structures |
| E1 | Results events are overpriced in India: the move priced into options before results exceeds the actual move | Implied vs actual move around each result (~65 events a month) | Consider a results-event strategy; otherwise keep avoiding results (F2) |

## Needs quote snapshots: from the first day of paper trading

The recorder saves bid/ask, sizes, volume, open interest and all pending buy and sell quantity every
15 minutes for the strikes the strategy trades.

| # | Hypothesis | Measure | If it holds |
|---|---|---|---|
| P1 | Heavy buying (option volume and open interest well above normal, IV rising while realised volatility does not) makes premium richer afterwards (owner's idea; demand-based option pricing) | IV / later realised vol after high vs normal demand | Add a demand-pressure signal |
| P2 | That same buying does not predict an adverse move in the stock (Pan and Poteshman found informed option buying in the US) | Tilt after high demand, split by broad (many strikes) vs concentrated (one strike, no news) | If concentrated buying predicts moves, exclude those stocks |
| P3 | Pending buy vs sell quantity in the order book predicts the next days' IV change | IV change after imbalance | Use it for entry timing |
| P4 | Market-wide client positioning (NSE participant-wise open interest, downloaded by hand) marks regimes when selling pays more | Premium by client net-long quintile | Use as a regime filter |

## Mapping to the earlier list

H1 in the strategy review is R1, H2 is D4, H3 is R4, H4 is D1 and D5, H5 is R5.
