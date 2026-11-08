# Option-selling study on real US data

Source: market_us.db; 48 stocks, 2024-01-03 to 2026-10-30; 2,851,594 option quotes. Held to expiry, no exit rules. Expiries 15-35 days out at entry. US data from DoltHub: real end-of-day bid/ask.

Completed expiries in the sample: 143. t-statistics need at least 8 (shown as n/a below that); treat anything under ~24 as anecdotal.

## A. Implied vs subsequently realised volatility (ATM, ~28 days)

| condition | symbol-days | median IV/RV | IV > RV share | mean IV-RV (vol pts) |
|---|---|---|---|---|
| all | 29186 | 1.09 | 0.62 | 0.47 |
| earnings in window: yes | 9850 | 1.14 | 0.67 | 2.00 |
| earnings in window: no | 19336 | 1.06 | 0.59 | -0.31 |

| condition | symbol-days | median IV/RV | IV > RV share | mean IV-RV (vol pts) |
|---|---|---|---|---|
| IV/HV20 1.00-1.25 | 6282 | 1.06 | 0.59 | -0.39 |
| IV/HV20 < 1.00 | 9435 | 1.06 | 0.59 | -0.49 |
| IV/HV20 >= 1.25 | 3619 | 1.07 | 0.61 | 0.27 |
| IV percentile 30-70 | 8441 | 1.06 | 0.59 | -0.71 |
| IV percentile < 30 | 6491 | 1.05 | 0.58 | -0.40 |
| IV percentile >= 70 | 2604 | 1.10 | 0.64 | 0.66 |
| IV percentile n/a | 1800 | 1.07 | 0.61 | 0.48 |
| shock in last 10 days: no | 12208 | 1.06 | 0.59 | -0.36 |
| shock in last 10 days: yes | 7128 | 1.06 | 0.60 | -0.24 |
| F3 and F4 and shock (our setup) | 134 | 1.12 | 0.59 | 1.07 | 

## B. Selling one option, held to expiry (no earnings in the window)

P&L per share as % of the stock price; t (NW) uses per-expiry means.

| side | |delta| | trades | win rate | mean P&L at mid (% of spot) | t (NW) | mean P&L selling at bid (% of spot) | t bid (NW) | mean return on premium (mid) |
|---|---|---|---|---|---|---|---|---|
| call | 0.05-0.15 | 27380 | 0.91 | -0.25 | -1.78 | -0.28 | -2.04 | -0.31 |
| call | 0.15-0.25 | 22940 | 0.82 | -0.46 | -2.63 | -0.52 | -3.03 | -0.32 |
| call | 0.25-0.35 | 20175 | 0.75 | -0.55 | -2.74 | -0.64 | -3.23 | -0.24 |
| call | 0.35-0.45 | 21043 | 0.70 | -0.67 | -2.67 | -0.79 | -3.16 | -0.17 |
| call | 0.45-0.55 | 21521 | 0.65 | -0.82 | -2.51 | -0.96 | -2.95 | -0.14 |
| put | 0.05-0.15 | 33409 | 0.93 | 0.16 | 2.25 | 0.12 | 1.66 | 0.41 |
| put | 0.15-0.25 | 24453 | 0.85 | 0.25 | 1.89 | 0.18 | 1.33 | 0.21 |
| put | 0.25-0.35 | 21827 | 0.78 | 0.37 | 1.95 | 0.27 | 1.40 | 0.17 |
| put | 0.35-0.45 | 20572 | 0.72 | 0.45 | 1.78 | 0.32 | 1.26 | 0.14 |
| put | 0.45-0.55 | 21154 | 0.68 | 0.62 | 2.01 | 0.46 | 1.48 | 0.11 | 

## C. 1-strike vertical spreads, short leg at |delta| 0.20-0.30, held to expiry

Median credit/width at mid 0.19, at natural prices 0.13; share with natural credit/width >= 0.25: 1.4%.

### Bull put spreads: P&L at mid as a share of width

| condition | trades | win rate | mean | t (NW) |
|---|---|---|---|---|
| all (earnings in window) | 13020 | 0.79 | -0.0085 | -0.29 |
| all (no earnings in window) | 20920 | 0.78 | 0.02 | 0.96 |
| IV/HV20 1.00-1.25 | 6834 | 0.78 | 0.01 | 0.43 |
| IV/HV20 < 1.00 | 10369 | 0.78 | 0.02 | 0.87 |
| IV/HV20 >= 1.25 | 3717 | 0.77 | 0.0058 | 0.22 |
| IV percentile 30-70 | 9322 | 0.78 | 0.03 | 1.12 |
| IV percentile < 30 | 6697 | 0.78 | -0.0094 | -0.33 |
| IV percentile >= 70 | 3321 | 0.76 | 0.05 | 1.67 |
| IV percentile n/a | 1580 | 0.81 | 0.05 | 1.44 |
| shock in last 10 days: no | 12856 | 0.77 | 0.01 | 0.46 |
| shock in last 10 days: yes | 8064 | 0.79 | 0.02 | 0.98 |
| F3 and F4 and shock (our setup) | 162 | 0.73 | -0.01 | -0.12 |
| F3 and F4, no recent shock | 533 | 0.73 | 0.04 | 1.11 | 

Same spreads at natural prices (sell bid, buy ask): mean P&L -0.069 of width, t (NW) -2.61 (no-earnings windows).

### Bear call spreads: P&L at mid as a share of width

| condition | trades | win rate | mean | t (NW) |
|---|---|---|---|---|
| all (earnings in window) | 11865 | 0.75 | -0.04 | -1.76 |
| all (no earnings in window) | 18892 | 0.76 | -0.05 | -3.05 |
| IV/HV20 1.00-1.25 | 5984 | 0.76 | -0.04 | -1.75 |
| IV/HV20 < 1.00 | 9555 | 0.76 | -0.05 | -2.43 |
| IV/HV20 >= 1.25 | 3353 | 0.75 | -0.04 | -2.22 |
| IV percentile 30-70 | 8400 | 0.77 | -0.06 | -2.54 |
| IV percentile < 30 | 5998 | 0.74 | -0.06 | -1.96 |
| IV percentile >= 70 | 2846 | 0.78 | -0.04 | -1.37 |
| IV percentile n/a | 1648 | 0.72 | -0.07 | -1.64 |
| shock in last 10 days: no | 11542 | 0.76 | -0.04 | -2.70 |
| shock in last 10 days: yes | 7350 | 0.75 | -0.05 | -2.42 |
| F3 and F4 and shock (our setup) | 125 | 0.74 | 0.03 | 0.78 |
| F3 and F4, no recent shock | 458 | 0.76 | -0.07 | -1.43 | 

Same spreads at natural prices (sell bid, buy ask): mean P&L -0.114 of width, t (NW) -6.84 (no-earnings windows).

### Wing width: does a wider spread survive the bid-ask? (no earnings in the window)

| spread | side | trades | median width % spot | median credit/width natural | win rate | P&L/width at mid | t mid | P&L/width at natural | t natural |
|---|---|---|---|---|---|---|---|---|---|
| next listed strike | bull put | 20920 | 2.12 | 0.14 | 0.78 | 0.02 | 0.96 | -0.06 | -2.61 |
| next listed strike | bear call | 18892 | 2.23 | 0.12 | 0.76 | -0.05 | -3.05 | -0.12 | -6.84 |
| wing ~3% of spot | bull put | 16896 | 2.78 | 0.14 | 0.78 | 0.02 | 0.87 | -0.04 | -1.71 |
| wing ~3% of spot | bear call | 14383 | 2.76 | 0.13 | 0.76 | -0.05 | -3.09 | -0.10 | -5.86 |
| wing ~6% of spot | bull put | 17109 | 5.92 | 0.13 | 0.79 | 0.01 | 0.79 | -0.0088 | -0.47 |
| wing ~6% of spot | bear call | 14330 | 5.79 | 0.11 | 0.76 | -0.05 | -2.96 | -0.07 | -4.29 |
| 0.15-0.20 delta short, wing ~6% | bull put | 8116 | 5.49 | 0.09 | 0.86 | 0.0098 | 0.61 | -0.0079 | -0.49 |
| 0.15-0.20 delta short, wing ~6% | bear call | 6467 | 5.27 | 0.07 | 0.83 | -0.04 | -2.97 | -0.06 | -4.08 | 

## D. Single 0.15-0.35 delta options by condition (P&L at mid, % of spot)

### Short puts

| condition | trades | win rate | mean | t (NW) |
|---|---|---|---|---|
| all (earnings in window) | 28099 | 0.84 | 0.12 | 0.38 |
| all (no earnings in window) | 46280 | 0.82 | 0.30 | 1.93 |
| IV/HV20 1.00-1.25 | 15053 | 0.81 | 0.22 | 1.05 |
| IV/HV20 < 1.00 | 22854 | 0.82 | 0.29 | 1.75 |
| IV/HV20 >= 1.25 | 8373 | 0.82 | 0.32 | 1.50 |
| IV percentile 30-70 | 20767 | 0.82 | 0.37 | 2.00 |
| IV percentile < 30 | 14846 | 0.82 | 0.15 | 0.71 |
| IV percentile >= 70 | 7183 | 0.80 | 0.78 | 4.01 |
| IV percentile n/a | 3484 | 0.83 | 0.42 | 2.15 |
| shock in last 10 days: no | 28632 | 0.81 | 0.25 | 1.39 |
| shock in last 10 days: yes | 17648 | 0.82 | 0.33 | 2.07 |
| F3 and F4 and shock (our setup) | 335 | 0.78 | 0.03 | 0.05 |
| F3 and F4, no recent shock | 1187 | 0.78 | 0.61 | 2.25 | 

### Short calls

| condition | trades | win rate | mean | t (NW) |
|---|---|---|---|---|
| all (earnings in window) | 26219 | 0.78 | -0.53 | -1.60 |
| all (no earnings in window) | 43115 | 0.79 | -0.50 | -2.71 |
| IV/HV20 1.00-1.25 | 13866 | 0.79 | -0.35 | -1.63 |
| IV/HV20 < 1.00 | 21568 | 0.79 | -0.60 | -1.69 |
| IV/HV20 >= 1.25 | 7681 | 0.78 | -0.45 | -2.46 |
| IV percentile 30-70 | 19214 | 0.80 | -0.76 | -2.76 |
| IV percentile < 30 | 13896 | 0.77 | -0.74 | -1.69 |
| IV percentile >= 70 | 6466 | 0.81 | 0.01 | 0.04 |
| IV percentile n/a | 3539 | 0.77 | -0.54 | -1.19 |
| shock in last 10 days: no | 26501 | 0.79 | -0.48 | -2.78 |
| shock in last 10 days: yes | 16614 | 0.79 | -0.55 | -1.95 |
| F3 and F4 and shock (our setup) | 310 | 0.81 | 0.18 | 0.54 |
| F3 and F4, no recent shock | 1088 | 0.77 | -1.34 | -1.57 | 

## E. Do support/resistance levels hold?

| level | event within 15 days | cases | actual rate | same-size move on any day | difference |
|---|---|---|---|---|---|
| support | close beyond the level (thesis stop) | 15688 | 0.67 | 0.67 | 0.0050 |
| support | close 0.25 ATR beyond it (short strike) | 15688 | 0.60 | 0.60 | 0.0033 |
| support | short strike reached, top-half scored levels | 7845 | 0.60 | 0.59 | 0.0059 |
| resistance | close beyond the level (thesis stop) | 15033 | 0.74 | 0.74 | 0.0015 |
| resistance | close 0.25 ATR beyond it (short strike) | 15033 | 0.67 | 0.68 | -0.0019 |
| resistance | short strike reached, top-half scored levels | 7518 | 0.66 | 0.67 | -0.01 | 

