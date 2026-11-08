# Option-selling study on real US data

Source: market_us.db; 48 stocks, 2024-01-03 to 2026-10-30; 2,851,594 option quotes. Held to expiry, no exit rules. Expiries 15-35 days out at entry.

## A. Implied vs subsequently realised volatility (ATM, ~28 days)

| condition | symbol-days | median IV/RV | IV > RV share | mean IV-RV (vol pts) |
|---|---|---|---|---|
| all | 29749 | 1.08 | 0.62 | 0.41 |
| earnings in window: yes | 10259 | 1.14 | 0.66 | 1.79 |
| earnings in window: no | 19490 | 1.06 | 0.59 | -0.31 |

| condition | symbol-days | median IV/RV | IV > RV share | mean IV-RV (vol pts) |
|---|---|---|---|---|
| IV/HV20 1.00-1.25 | 6342 | 1.06 | 0.59 | -0.40 |
| IV/HV20 < 1.00 | 9473 | 1.06 | 0.59 | -0.48 |
| IV/HV20 >= 1.25 | 3675 | 1.07 | 0.61 | 0.27 |
| IV percentile 30-70 | 8510 | 1.06 | 0.59 | -0.70 |
| IV percentile < 30 | 6574 | 1.05 | 0.58 | -0.41 |
| IV percentile >= 70 | 2606 | 1.10 | 0.64 | 0.67 |
| IV percentile n/a | 1800 | 1.07 | 0.61 | 0.48 |
| shock in last 10 days: no | 12277 | 1.06 | 0.59 | -0.36 |
| shock in last 10 days: yes | 7213 | 1.07 | 0.60 | -0.23 |
| F3 and F4 and shock (our setup) | 134 | 1.12 | 0.59 | 1.07 | 

## B. Selling one option, held to expiry (no earnings in the window)

P&L per share as % of the stock price; t (NW) uses per-expiry means.

| side | |delta| | trades | win rate | mean P&L at mid (% of spot) | t (NW) | mean P&L selling at bid (% of spot) | t bid (NW) | mean return on premium (mid) |
|---|---|---|---|---|---|---|---|---|
| call | 0.05-0.15 | 27569 | 0.91 | -0.23 | -1.72 | -0.27 | -1.99 | -0.30 |
| call | 0.15-0.25 | 23122 | 0.82 | -0.44 | -2.56 | -0.51 | -2.97 | -0.31 |
| call | 0.25-0.35 | 20372 | 0.75 | -0.52 | -2.63 | -0.62 | -3.13 | -0.24 |
| call | 0.35-0.45 | 21285 | 0.70 | -0.63 | -2.58 | -0.76 | -3.10 | -0.16 |
| call | 0.45-0.55 | 21785 | 0.65 | -0.79 | -2.44 | -0.93 | -2.90 | -0.13 |
| put | 0.05-0.15 | 33640 | 0.93 | 0.17 | 2.38 | 0.13 | 1.78 | 0.42 |
| put | 0.15-0.25 | 24659 | 0.85 | 0.26 | 2.03 | 0.19 | 1.46 | 0.22 |
| put | 0.25-0.35 | 22063 | 0.78 | 0.39 | 2.10 | 0.29 | 1.53 | 0.17 |
| put | 0.35-0.45 | 20824 | 0.72 | 0.48 | 1.93 | 0.34 | 1.39 | 0.14 |
| put | 0.45-0.55 | 21411 | 0.68 | 0.65 | 2.14 | 0.48 | 1.60 | 0.12 | 

## C. 1-strike vertical spreads, short leg at |delta| 0.20-0.30, held to expiry

Median credit/width at mid 0.19, at natural prices 0.13; share with natural credit/width >= 0.25: 1.4%.

### Bull put spreads: P&L at mid as a share of width

| condition | trades | win rate | mean | t (NW) |
|---|---|---|---|---|
| all (earnings in window) | 13495 | 0.79 | -0.0093 | -0.32 |
| all (no earnings in window) | 21128 | 0.78 | 0.02 | 1.10 |
| IV/HV20 1.00-1.25 | 6913 | 0.78 | 0.01 | 0.58 |
| IV/HV20 < 1.00 | 10418 | 0.78 | 0.02 | 0.99 |
| IV/HV20 >= 1.25 | 3797 | 0.78 | 0.0080 | 0.30 |
| IV percentile 30-70 | 9415 | 0.78 | 0.03 | 1.32 |
| IV percentile < 30 | 6810 | 0.78 | -0.0065 | -0.24 |
| IV percentile >= 70 | 3323 | 0.76 | 0.05 | 1.78 |
| IV percentile n/a | 1580 | 0.81 | 0.05 | 1.44 |
| shock in last 10 days: no | 12955 | 0.77 | 0.01 | 0.57 |
| shock in last 10 days: yes | 8173 | 0.79 | 0.02 | 1.15 |
| F3 and F4 and shock (our setup) | 162 | 0.73 | -0.01 | -0.12 |
| F3 and F4, no recent shock | 533 | 0.73 | 0.04 | 1.11 | 

Same spreads at natural prices (sell bid, buy ask): mean P&L -0.070 of width, t (NW) -2.61 (no-earnings windows).

### Bear call spreads: P&L at mid as a share of width

| condition | trades | win rate | mean | t (NW) |
|---|---|---|---|---|
| all (earnings in window) | 12314 | 0.76 | -0.04 | -1.69 |
| all (no earnings in window) | 19054 | 0.76 | -0.05 | -3.00 |
| IV/HV20 1.00-1.25 | 6038 | 0.76 | -0.03 | -1.68 |
| IV/HV20 < 1.00 | 9595 | 0.76 | -0.05 | -2.52 |
| IV/HV20 >= 1.25 | 3421 | 0.76 | -0.04 | -2.13 |
| IV percentile 30-70 | 8469 | 0.77 | -0.06 | -2.66 |
| IV percentile < 30 | 6089 | 0.74 | -0.06 | -1.93 |
| IV percentile >= 70 | 2848 | 0.78 | -0.03 | -1.22 |
| IV percentile n/a | 1648 | 0.72 | -0.07 | -1.64 |
| shock in last 10 days: no | 11622 | 0.76 | -0.04 | -2.55 |
| shock in last 10 days: yes | 7432 | 0.75 | -0.05 | -2.46 |
| F3 and F4 and shock (our setup) | 125 | 0.74 | 0.03 | 0.78 |
| F3 and F4, no recent shock | 458 | 0.76 | -0.07 | -1.43 | 

Same spreads at natural prices (sell bid, buy ask): mean P&L -0.112 of width, t (NW) -6.90 (no-earnings windows).

### Wing width: does a wider spread survive the bid-ask? (no earnings in the window)

| spread | side | trades | median width % spot | median credit/width natural | win rate | P&L/width at mid | t mid | P&L/width at natural | t natural |
|---|---|---|---|---|---|---|---|---|---|
| next listed strike | bull put | 21128 | 2.12 | 0.14 | 0.78 | 0.02 | 1.10 | -0.06 | -2.61 |
| next listed strike | bear call | 19054 | 2.23 | 0.12 | 0.76 | -0.05 | -3.00 | -0.12 | -6.90 |
| wing ~3% of spot | bull put | 17061 | 2.78 | 0.14 | 0.78 | 0.02 | 1.00 | -0.03 | -1.67 |
| wing ~3% of spot | bear call | 14501 | 2.76 | 0.13 | 0.76 | -0.05 | -3.10 | -0.10 | -5.95 |
| wing ~6% of spot | bull put | 17273 | 5.92 | 0.13 | 0.79 | 0.02 | 0.92 | -0.0069 | -0.38 |
| wing ~6% of spot | bear call | 14452 | 5.79 | 0.11 | 0.77 | -0.05 | -2.85 | -0.07 | -4.21 |
| 0.15-0.20 delta short, wing ~6% | bull put | 8167 | 5.49 | 0.09 | 0.86 | 0.01 | 0.74 | -0.0063 | -0.40 |
| 0.15-0.20 delta short, wing ~6% | bear call | 6512 | 5.27 | 0.07 | 0.83 | -0.04 | -2.84 | -0.06 | -3.96 | 

## D. Single 0.15-0.35 delta options by condition (P&L at mid, % of spot)

### Short puts

| condition | trades | win rate | mean | t (NW) |
|---|---|---|---|---|
| all (earnings in window) | 29256 | 0.84 | 0.10 | 0.34 |
| all (no earnings in window) | 46722 | 0.82 | 0.32 | 2.08 |
| IV/HV20 1.00-1.25 | 15206 | 0.82 | 0.24 | 1.20 |
| IV/HV20 < 1.00 | 22966 | 0.82 | 0.30 | 1.86 |
| IV/HV20 >= 1.25 | 8550 | 0.82 | 0.34 | 1.60 |
| IV percentile 30-70 | 20972 | 0.82 | 0.39 | 2.17 |
| IV percentile < 30 | 15077 | 0.82 | 0.17 | 0.83 |
| IV percentile >= 70 | 7189 | 0.80 | 0.79 | 4.11 |
| IV percentile n/a | 3484 | 0.83 | 0.42 | 2.15 |
| shock in last 10 days: no | 28843 | 0.81 | 0.27 | 1.52 |
| shock in last 10 days: yes | 17879 | 0.83 | 0.34 | 2.22 |
| F3 and F4 and shock (our setup) | 335 | 0.78 | 0.03 | 0.05 |
| F3 and F4, no recent shock | 1187 | 0.78 | 0.61 | 2.25 | 

### Short calls

| condition | trades | win rate | mean | t (NW) |
|---|---|---|---|---|
| all (earnings in window) | 27279 | 0.78 | -0.50 | -1.53 |
| all (no earnings in window) | 43494 | 0.79 | -0.47 | -2.62 |
| IV/HV20 1.00-1.25 | 14009 | 0.79 | -0.33 | -1.54 |
| IV/HV20 < 1.00 | 21661 | 0.79 | -0.59 | -1.69 |
| IV/HV20 >= 1.25 | 7824 | 0.78 | -0.42 | -2.35 |
| IV percentile 30-70 | 19389 | 0.80 | -0.74 | -2.75 |
| IV percentile < 30 | 14095 | 0.77 | -0.70 | -1.65 |
| IV percentile >= 70 | 6471 | 0.81 | 0.03 | 0.10 |
| IV percentile n/a | 3539 | 0.77 | -0.54 | -1.19 |
| shock in last 10 days: no | 26675 | 0.79 | -0.45 | -2.63 |
| shock in last 10 days: yes | 16819 | 0.79 | -0.53 | -1.92 |
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

