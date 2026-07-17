import pandas as pd
import numpy as np
import os
import yfinance as yf
from returns_engine import load_and_calculate_returns


def run_dynamic_strategy():
    # 1. Load our aligned simple return matrix for individual stocks
    prices, simple_ret, _ = load_and_calculate_returns()

    # Exclude any index-level columns if they exist in local_db
    stock_cols = [col for col in simple_ret.columns if col not in ["^NSEI", "NSEI"]]
    stock_returns = simple_ret[stock_cols]
    stock_prices = prices[stock_cols]

    # 2. Fetch the Nifty 50 Benchmark Returns directly on-the-fly to prevent KeyErrors
    print("\nFetching ^NSEI (Nifty 50) benchmark data for alignment...")
    bench_data = yf.download(
        "^NSEI",
        start=prices.index.min().strftime('%Y-%m-%d'),
        end=prices.index.max().strftime('%Y-%m-%d'),
        auto_adjust=False,
        multi_level_index=False,
        progress=False
    )

    if not bench_data.empty:
        # Fallback to 'Close' if 'Adj Close' doesn't exist
        bench_col = 'Adj Close' if 'Adj Close' in bench_data.columns else 'Close'
        bench_prices = bench_data[bench_col].rename("Nifty_50")
        # Align index with our master stock matrix
        bench_prices = bench_prices.reindex(prices.index).ffill()
        bench_returns = bench_prices.pct_change().fillna(0.0)
    else:
        # Emergency backup: If download fails, create an equal-weighted proxy of our universe
        print("⚠️ Warning: Could not download ^NSEI. Creating an equal-weighted benchmark proxy...")
        bench_returns = stock_returns.mean(axis=1)

    # 3. Get quarterly rebalancing dates (first trading day of each quarter)
    all_dates = stock_returns.index
    quarterly_dates = stock_returns.resample('QS').first().index

    # Keep only dates that exist in our trading dataset
    rebalance_dates = [all_dates[all_dates.get_indexer([d], method='bfill')[0]] for d in quarterly_dates]
    rebalance_dates = sorted(list(set(rebalance_dates)))

    # Initialize tracking variables
    portfolio_nav = pd.Series(index=all_dates, dtype=float)
    portfolio_nav.iloc[0] = 100.0  # Start NAV at 100

    current_weights = {}
    active_portfolio = []

    print("\n--- Running Dynamic Quarterly Rebalancing Engine ---")

    for i in range(len(all_dates)):
        current_date = all_dates[i]

        # Check if today is a rebalancing day
        if current_date in rebalance_dates:
            idx = all_dates.get_loc(current_date)
            # We look back 126 trading days (~6 months) to calculate selection metrics
            if idx >= 126:
                lookback_prices = stock_prices.iloc[idx - 126:idx]

                # Metric 1: 6-Month Momentum (cumulative return)
                momentum = (lookback_prices.iloc[-1] / lookback_prices.iloc[0]) - 1

                # Metric 2: 6-Month Volatility (daily standard dev annualized)
                volatility = lookback_prices.pct_change().std() * np.sqrt(252)

                # Factor Score: Momentum divided by Volatility (Risk-Adjusted Momentum)
                # Filter out stocks that weren't trading yet (NaNs)
                risk_adj_momentum = (momentum / volatility).dropna()

                # Select top 10 stocks
                selected_stocks = risk_adj_momentum.nlargest(10).index.tolist()

                # Assign equal weights (10% each)
                current_weights = {stock: 0.10 for stock in selected_stocks}
                active_portfolio = selected_stocks

                print(f"Rebalanced on {current_date.date()}: Selected {active_portfolio[:5]}...")
            else:
                # If we don't have 6 months of historical data yet, default to top Nifty heavyweights
                default_picks = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "LT", "ITC", "M&M", "SUNPHARMA",
                                 "BHARTIARTL"]
                current_weights = {stock: 0.10 for stock in default_picks if stock in stock_cols}
                active_portfolio = list(current_weights.keys())

        # Calculate daily return of our dynamic portfolio
        daily_returns_today = stock_returns.loc[current_date, active_portfolio]
        # Handle cases where some selected stock might have a NaN return on a specific day
        daily_returns_today = daily_returns_today.fillna(0.0)

        # Calculate daily weighted return
        weighted_return = sum(daily_returns_today[stock] * current_weights[stock] for stock in active_portfolio)

        # Update NAV
        if i > 0:
            portfolio_nav.iloc[i] = portfolio_nav.iloc[i - 1] * (1 + weighted_return)

    # Calculate Benchmark NAV (Nifty 50 index)
    bench_nav = (1 + bench_returns).cumprod() * 100

    # Calculate performance metrics
    total_port_return = (portfolio_nav.iloc[-1] / portfolio_nav.iloc[0]) - 1
    total_bench_return = (bench_nav.iloc[-1] / bench_nav.iloc[0]) - 1
    active_return = total_port_return - total_bench_return

    print("\n" + "=" * 50)
    print("DYNAMIC PORTFOLIO VS BENCHMARK PERFORMANCE")
    print("=" * 50)
    print(f"Dynamic Portfolio Return: {total_port_return * 100:.2f}%")
    print(f"Nifty 50 Benchmark Return: {total_bench_return * 100:.2f}%")
    print(f"Active Return (Alpha):      {active_return * 100:.2f}%")
    print("=" * 50)

    # Save the NAV curves to a CSV for the Streamlit dashboard
    comparison_df = pd.DataFrame({
        "Dynamic_Portfolio": portfolio_nav,
        "Nifty_50": bench_nav
    }, index=all_dates)
    comparison_df.to_csv("portfolio_nav_comparison.csv")
    print("NAV curves successfully saved to 'portfolio_nav_comparison.csv'")


if __name__ == "__main__":
    run_dynamic_strategy()