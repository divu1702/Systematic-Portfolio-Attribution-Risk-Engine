import os
import pandas as pd
import numpy as np

DATA_DIR = "local_db"


def load_and_calculate_returns():
    """
    Loads raw adjusted close parquet files, aligns them by date,
    and calculates both simple and log returns.
    """
    all_prices = {}

    # 1. Read all files in the local database
    files = [f for f in os.listdir(DATA_DIR) if f.endswith(".parquet")]

    print(f"Loading {len(files)} files from local_db...")
    for file in files:
        symbol = file.replace(".parquet", "")
        file_path = os.path.join(DATA_DIR, file)

        # Load parquet file
        df = pd.read_parquet(file_path)

        # Extract the price series
        all_prices[symbol] = df['Adj_Close']

    # 2. Align all series into a single master DataFrame
    # Using 'outer' join to preserve dates even if some stocks weren't trading yet
    price_matrix = pd.DataFrame(all_prices).sort_index()

    # 3. Handle Missing Data (For new listings)
    # We forward fill ('ffill') to carry forward the last known price,
    # but keep NaNs for periods before a stock was listed.
    price_matrix = price_matrix.ffill()

    # 4. Calculate daily simple returns (for Portfolio Return aggregation)
    simple_returns = price_matrix.pct_change()

    # 5. Calculate daily log returns (for statistical modeling & Risk Engine)
    log_returns = np.log(price_matrix / price_matrix.shift(1))

    print(f"Master Price Matrix Shape: {price_matrix.shape}")
    print(f"Successfully processed returns from {price_matrix.index.min().date()} to {price_matrix.index.max().date()}")

    return price_matrix, simple_returns, log_returns


if __name__ == "__main__":
    prices, simple_ret, log_ret = load_and_calculate_returns()

    # Quick sanity check: Print first 5 rows of returns for a few stocks
    sample_cols = [c for c in log_ret.columns if c in ["RELIANCE", "TCS", "HDFCBANK", "JIOFIN"]]
    print("\nSample Log Returns (First 5 Rows):")
    print(log_ret[sample_cols].head())