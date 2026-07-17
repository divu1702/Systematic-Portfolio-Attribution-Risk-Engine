import pandas as pd
import numpy as np
from scipy.stats import norm
from src.returns_engine import load_and_calculate_returns


def run_risk_engine():
    # 1. Load our clean daily returns from the previous stage
    _, simple_ret, log_ret = load_and_calculate_returns()

    # 2. Define a mock active portfolio of 10 Nifty stocks
    portfolio_weights = {
        "RELIANCE": 0.15,
        "HDFCBANK": 0.15,
        "TCS": 0.10,
        "INFY": 0.10,
        "ICICIBANK": 0.10,
        "BHARTIARTL": 0.10,
        "L&T": 0.10,  # Note: Checked as 'LT' in our database
        "M&M": 0.10,
        "ITC": 0.05,
        "SUNPHARMA": 0.05
    }

    # Map tickers to match the exact database columns (e.g., L&T is represented as 'LT')
    ticker_mapping = {"L&T": "LT"}
    portfolio_weights = {ticker_mapping.get(k, k): v for k, v in portfolio_weights.items()}

    active_tickers = list(portfolio_weights.keys())
    weights = np.array([portfolio_weights[t] for t in active_tickers])

    # Slice our return matrices to only include our active portfolio assets
    # Dropping rows where any of our active assets don't have data yet to keep math clean
    portfolio_log_ret = log_ret[active_tickers].dropna()
    portfolio_simple_ret = simple_ret[active_tickers].dropna()

    print(f"\n--- Running Portfolio Risk Analysis ({len(active_tickers)} Assets) ---")

    # 3. Calculate Daily Portfolio Returns (Weighted sum of simple returns)
    daily_portfolio_returns = portfolio_simple_ret.dot(weights)

    # 4. Compute Annualized Volatility using Matrix Multiplication (w^T * Sigma * w)
    # 252 trading days in a standard Indian market year
    daily_cov_matrix = portfolio_log_ret.cov()
    annualized_cov_matrix = daily_cov_matrix * 252

    portfolio_variance = np.dot(weights.T, np.dot(annualized_cov_matrix, weights))
    portfolio_volatility = np.sqrt(portfolio_variance)

    # 5. Compute Value at Risk (VaR) - $1,000,000 portfolio size
    portfolio_value = 1000000  # Example in USD or INR
    confidence_level = 0.99

    # --- METHOD A: Parametric (Variance-Covariance) VaR ---
    # Daily portfolio standard deviation (volatility)
    daily_portfolio_vol = np.sqrt(np.dot(weights.T, np.dot(daily_cov_matrix, weights)))
    mean_daily_return = daily_portfolio_returns.mean()

    # Find the z-score for 99% confidence (which is 2.33)
    z_score = norm.ppf(1 - confidence_level)
    parametric_var_pct = -(mean_daily_return + z_score * daily_portfolio_vol)
    parametric_var_amount = parametric_var_pct * portfolio_value

    # --- METHOD B: Historical Simulation VaR ---
    # Sort actual historical daily returns and find the 1st percentile
    historical_var_pct = -np.percentile(daily_portfolio_returns, (1 - confidence_level) * 100)
    historical_var_amount = historical_var_pct * portfolio_value

    # 6. Print Results
    print(f"Annualized Portfolio Volatility: {portfolio_volatility * 100:.2f}%")
    print(f"\nValue at Risk (99% 1-Day VaR) on a 1,000,000 Portfolio:")
    print(f"  Method A (Parametric):   {parametric_var_pct * 100:.2f}% (Amt: {parametric_var_amount:,.2f})")
    print(f"  Method B (Historical):   {historical_var_pct * 100:.2f}% (Amt: {historical_var_amount:,.2f})")

    # 7. Check for anomalies / "Fat Tails"
    # If Historical VaR is significantly higher than Parametric, the data has fat tails
    ratio = historical_var_pct / parametric_var_pct
    print(f"\nHistorical-to-Parametric VaR Ratio: {ratio:.2f}x")
    if ratio > 1.1:
        print(
            "💡 Operational Insight: The asset returns exhibit high kurtosis (fat tails). Parametric models understate extreme loss risks here.")

    return daily_portfolio_returns, portfolio_volatility


if __name__ == "__main__":
    run_risk_engine()
