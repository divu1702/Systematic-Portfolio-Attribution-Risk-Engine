# Is our logic suited to selling stock options? A research review

8 Nov 2026. Literature plus an empirical study on 2.85 million real US option quotes (48 stocks,
Jan 2024 - Oct 2026). Raw output: [results_us.md](results_us.md); code: [option_selling_study.py](option_selling_study.py).
This review changes no settings. It ends with a draft of refined rules for the owner to review
before any further backtests.

## Verdict

The **premise** is sound: selling option premium, defined risk, avoiding results dates and
sizing small is how premium sellers earn the volatility risk premium, and Indian single-stock
options appear to carry a larger premium than US ones. But five design choices work against the
economics of option selling, and the real-data evidence says they, not bad luck, are why the
backtests found no trades or lost money:

1. **Structure.** Narrow verticals (short 0.20-0.30 delta, long one strike further) give back most
   of the premium: the long wing is priced at a higher IV (skew), and the bid-ask is paid on two legs.
   At natural prices they lost 6% (bull puts) to 12% (bear calls) of the width per trade.
2. **The richness test (F3).** IV / HV20 did not identify rich IV: IV above the 1.25 threshold was no
   richer relative to the volatility that followed than any other day. IV percentile (F4) did.
3. **Timing (F5).** Selling soon after a large price move removed the benefit of high IV. The same
   high-IV setups without a recent shock did markedly better.
4. **Direction and levels (F6, thesis stop).** Our support/resistance levels held no better than
   chance, and the thesis stop fires in most trades.
5. **Exits.** The 50% profit target and a 2x-credit stop on natural prices cut the small edge further.

## 1. What the research says

**The volatility risk premium is concentrated in index options; for US single stocks it is small.**
Index IV exceeds later realised volatility by a wide margin, while single-stock options carry little
or no premium on average ([Driessen, Maenhout and Vilkov, via OptionMetrics/CXO summaries](https://www.cxoadvisory.com/?p=2110);
[Linn, citing Carr and Wu and Bakshi and Kapadia](https://www.isenberg.umass.edu/sites/default/files/2020-13/LinnJMP.pdf)).
Demand explains part of it: end users are net long index options, especially puts, and at times net
short single-stock options ([Garleanu, Pedersen and Poteshman, 2009](https://www.nber.org/papers/w11843.pdf)).

**India looks different.** Jain, Varma and Agarwalla (2019, *Journal of Futures Markets*) report that
IV on Indian single-stock options exceeds subsequent realised volatility by more than a rational risk
premium would explain (as reported in search summaries of [Varma's blog post](https://www.jrvarma.in/blog/Y2018-19/smile-paper.html);
I could not open the blog or the paper from this environment, so magnitudes are unverified). SEBI's FY25-26 study found option sellers were the only
group of individual traders with a positive median return, with about 44% loss-making against ~90% for
option buyers, but with severe tail losses: losing sellers lost ₹51.7 lakh on average
([summary](https://openthemagazine.com/business/sebi-fo-loss-study-explained-why-9-in-10-retail-traders-lost-91685-crore-in-fy26);
[Policy Edge](https://www.policyedge.in/p/nearly-9-in-10-individual-fo-traders-lost-money-as-participation-fell-18-sebi-studies)).

**Where single-stock premium is richest:**
- *Large IV-versus-HV gaps across stocks.* Sorting stocks by the gap between historical and implied
  volatility predicts option returns: options with IV far above HV underperform, so selling them pays,
  though transaction costs cut the profit sharply ([Goyal and Saretto, 2009](https://econpapers.repec.org/RePEc:eee:jfinec:v:94:y:2009:i:2:p:310-326);
  [working paper](https://www.erim.eur.nl/fileadmin/erim_content/documents/10)). This is a
  *cross-sectional* ranking, not an absolute threshold like our 1.25.
- *Stocks with high idiosyncratic volatility*, whose options are more overpriced
  ([Cao and Han, 2013](https://econpapers.repec.org/RePEc:eee:jfinec:v:108:y:2013:i:1:p:231-249)).
- *Earnings.* IV runs up before results and collapses after; straddles lose on average around
  announcements, so event volatility is usually overpriced, but with gap risk
  ([Dubinsky and Johannes](https://business.columbia.edu/sites/default/files-efs/pubfiles/6051/DJ_2006.pdf)).
- *Persistence.* Options whose straddles lost money in the past keep losing for 6-36 months
  ([Heston, Jones, Khorram, Li and Mo, 2023](https://alphaarchitect.com/option-momentum/)), a possible
  stock-selection signal.

**What the research says about selling right after a shock.** Options traders *underreact* to a
single day's change in variance and overreact only to runs of mostly rising or falling variance
([Poteshman, 2001](https://ideas.repec.org/a/bla/jfinan/v56y2001i3p851-876.html); see also
[Stein, 1989](https://stein.scholars.harvard.edu/publications/overreactions-options-market)). So the day
after one big move, IV is if anything too *low*, not too high, and realised volatility tends to stay
elevated. Our F5 is built on the opposite assumption.

**Support and resistance.** The strongest evidence is from currency markets, where published levels
had predictive power that Osler traced to clustered customer orders ([Osler, NY Fed](https://www.newyorkfed.org/medialibrary/media/research/epr/00v06n2/0007osle.html));
evidence for individual stocks is thin.

**Managing exits.** Industry studies disagree on the 50% profit target: one large SPY study found it
gave the best return on capital, while a 2x-credit stop lowered both win rate (75% to 65%) and average
profit ([Luckbox](https://luckboxmagazine.com/?p=7123)); another found holding to expiry compounded
better ([Early Retirement Now](https://earlyretirementnow.com/2020/06/17/passive-income-through-option-writing-part-5));
an Option Alpha study found targets and stops smooth the ride but give up upside
([Option Alpha](https://optionalpha.com/blog/spy-put-credit-spread-backtest)). None is peer-reviewed.

**Indian rule changes that matter.** From May 2026 the calendar-spread margin benefit for single-stock
derivatives is removed on expiry day ([Groww summary](https://groww.in/blog/sebi-removes-calendar-spread-margin-benefit-expiry-day));
our time stop already exits before expiry week. Stocks can be dropped from F&O when they fail
eligibility, with open contracts allowed to run to expiry ([Kotak summary](https://www.kotakneo.com/news/stocks/nse-remove-exide-nuvama-wealth-fo-segment-july-2026/)).

## 2. What our data says (US, Jan 2024 - Oct 2026, held to expiry)

**A. Implied vs subsequently realised volatility** (ATM, ~28 days). Median IV/RV 1.08: a premium, but
small. Windows containing earnings: 1.14. Without earnings, by our filters:

| Condition | Median IV / later RV | IV above later RV |
|---|---|---|
| IV/HV20 below 1.00 | 1.06 | 59% |
| IV/HV20 at or above 1.25 (F3) | 1.07 | 61% |
| IV percentile at or above 70 (F4) | **1.10** | **64%** |
| Large move in the last 10 days (F5) | 1.07 | 60% |

F3 and F5 pick days no richer than any other; F4 picks somewhat richer ones.

**B. Selling a single option** (no earnings in the window, P&L as % of the stock price per cycle):
short puts made +0.2% to +0.7% (t about 2), short calls lost 0.2% to 0.8% (t about -2.5). This was a
strong bull market, so the put/call split mostly reflects the trend, not option pricing.

**C. Our structure: vertical spreads, short leg 0.20-0.30 delta.** Median credit/width 0.19 at mid and
0.13 at natural prices; only **1.4%** of spreads reached F8's 25%.

| Wing (no earnings in window) | Bull put, P&L/width at natural | Bear call, P&L/width at natural |
|---|---|---|
| Next listed strike (~2% of spot) | -6% (t -2.6) | -12% (t -6.9) |
| ~3% of spot | -3% (t -1.7) | -10% (t -6.0) |
| ~6% of spot | **-0.7% (t -0.4)** | -7% (t -4.2) |

At mid prices bull puts made about +2% of width; the bid-ask turns that into a loss for narrow wings.
Wider wings roughly halve or remove the drag.

**D. Conditions that changed short-put results** (0.15-0.35 delta, P&L % of spot, no earnings):

| Condition | Mean | t |
|---|---|---|
| All | +0.32 | 2.1 |
| IV percentile at or above 70 | **+0.79** | **4.1** |
| F3 and F4 *without* a recent shock | +0.61 | 2.3 |
| F3 and F4 *with* a recent shock (our setup) | +0.03 | 0.1 |
| Earnings inside the window | +0.10 | 0.3 |

For calls, IV percentile at or above 70 lifted results from -0.47 to +0.03. Holding through earnings
paid less, despite the higher IV, so avoiding results (F2) is supported for out-of-the-money selling.

**E. Support/resistance.** For a support within 1.5 ATR below the price, the close reached the level
within 15 days 67% of the time, and 0.25 ATR beyond it 60%: in both cases the same as a move of that
size on any day of the same stock (difference under 1 point). Higher-scored levels did no better.
Resistance: the same. Our levels carried no measurable information.

**Caveats.** US market, not NSE; one strongly rising market; end-of-day quotes, wider than intraday
ones, so natural-price figures are pessimistic; held to expiry, so no exit rules; many conditions
were examined, so the strongest finding (IV percentile) still needs out-of-sample confirmation.

## 3. Our rules, one by one

| Rule | Evidence | Suggestion |
|---|---|---|
| Universe: all liquid F&O stocks | Fine; Indian single-stock premium may be large | Keep |
| F2 avoid events | OTM selling through results paid less | Keep, with the estimated-results rule |
| F3 IV/HV20 >= 1.25 | No better than other days; HV20 is backward-looking | Replace with IV vs a forward volatility forecast, or a cross-sectional rank |
| F4 IV percentile >= 70 | The strongest signal found | Make it the primary richness test |
| F5 sell after a shock | Literature: IV underreacts to single shocks; data: shock removed the benefit | Replace with "shock has passed": no large move for N days and recent realised vol below IV |
| Direction from levels | Levels held no better than chance | Use trend (price vs a moving average) or sell both sides |
| F6 strike beyond the level | Same | Drop; keep distance and delta (F7) |
| F7 delta 0.20-0.30 | Reasonable | Consider 0.15-0.25 with wider wings |
| F8 credit/width >= 25% | Reached by 1.4% of spreads; conflicts with F7 | Replace with a friction test: credit large relative to the two legs' bid-ask |
| Wing: 1-4 strike steps | Narrow wings lose to the bid-ask | Choose the wing by % of spot (~5-6%) |
| Exits: 50% target, 2x stop on natural prices, thesis stop | Target halves the modelled edge; stops fire on quote noise; thesis stop fires in most trades | 75% target or none; stop on mid prices or on the underlying crossing the short strike; drop the thesis stop |
| Sizing by risk at the stop | Tiny credits became 30-60-contract positions | With wider wings, size by max loss, or keep stop sizing with a minimum credit and a contract cap |

## 4. Draft refined logic (v2, for review, not implemented)

1. **Universe:** all F&O stocks with absolute option liquidity (unchanged).
2. **Rich IV:** IV percentile >= 70 against the stock's own year, and IV above a forward-looking
   realised-volatility forecast (EWMA or HAR) by a margin; optionally trade only the top-ranked stocks
   by IV richness each day, Goyal-Saretto style.
3. **Timing:** no large (>= 1.5 ATR) move in the last 5-10 sessions, and 5-day realised volatility below IV.
4. **Events:** unchanged (F2 with estimated results).
5. **Direction:** bull put spreads when the stock is above its 50-day average, bear call spreads when below;
   optionally both sides (iron condor) when there is no clear trend.
6. **Strikes:** short leg 0.15-0.25 delta and at least 1 ATR from spot; long leg about 5-6% of spot further out.
7. **Credit quality:** net credit at natural prices at least 3x the combined bid-ask half-spreads and above
   a minimum in rupees per lot; path EV > 0 as now.
8. **Exits:** profit target 75% (or none); hard stop when the stock closes beyond the short strike or the
   mid-price cost to close reaches 2.5-3x the credit; time stop 5 trading days before expiry; event exit.
9. **Sizing:** by max loss, 1-2% of equity per trade, more and smaller positions across sectors;
   portfolio caps on net delta.

## 5. How to test it without fooling ourselves

Write the hypotheses down first, then test each once on data we have not looked at:

- H1 IV percentile >= 70 improves results. H2 dropping the fresh-shock requirement improves results.
  H3 ~5-6% wings beat 1-strike wings at natural prices. H4 trend direction beats level direction.
  H5 a 75% target with mid-price stops beats a 50% target with natural-price stops.
- Untouched US data: DoltHub has 2019-2023, which none of this work used (about 2-3 hours to load).
- Indian data: the live recorder, after about 3 months of paper trading.
- Judge v2 on the second half of each period after choosing any thresholds on the first half.
