import os
import yfinance as yf
import pandas as pd

# The current 50 Nifty symbols from your list (using standard NSE format)
nifty_50_symbols = [
    "RELIANCE", "JIOFIN", "TECHM", "ICICIBANK", "AXISBANK",
    "HDFCBANK", "INFY", "BAJFINANCE", "TCS", "BHARTIARTL",
    "SBIN", "LT", "KOTAKBANK", "HCLTECH", "M&M",
    "ETERNAL", "SUNPHARMA", "BAJAJ-AUTO", "ITC", "WIPRO",
    "HINDALCO", "TATASTEEL", "EICHERMOT", "JSWSTEEL", "SHRIRAMFIN",
    "MARUTI", "ADANIENT", "INDIGO", "POWERGRID", "TITAN",
    "ULTRACEMCO", "BEL", "NTPC", "TMPV", "DRREDDY",
    "HDFCLIFE", "HINDUNILVR", "TRENT", "NESTLEIND", "GRASIM",
    "SBILIFE", "COALINDIA", "ADANIPORTS", "MAXHEALTH", "ONGC",
    "TATACONSUM", "BAJAJFINSV", "APOLLOHOSP", "ASIANPAINT", "CIPLA"
]

DATA_DIR = "local_db"
os.makedirs(DATA_DIR, exist_ok=True)

START_DATE = "2021-01-01"
END_DATE = "2026-07-17"

print("--- Starting Robust Data Ingestion Pipeline ---")

for symbol in nifty_50_symbols:
    ticker = f"{symbol}.NS"
    file_path = os.path.join(DATA_DIR, f"{symbol}.parquet")

    print(f"Fetching {symbol}...")

    # We pass multi_level_index=False to force a flat column index
    df = yf.download(
        ticker,
        start=START_DATE,
        end=END_DATE,
        auto_adjust=False,
        multi_level_index=False,
        progress=False
    )

    # --- Smart Fallbacks for Rebrands / Demergers ---
    if df.empty or len(df) < 100:
        if symbol == "ETERNAL":
            print("  ↳ Low/No data for ETERNAL. Attempting historical fallback to ZOMATO...")
            df = yf.download(
                "ZOMATO.NS",
                start=START_DATE,
                end=END_DATE,
                auto_adjust=False,
                multi_level_index=False,
                progress=False
            )
        elif symbol == "TMPV":
            print("  ↳ TMPV is a new listing. Attempting historical proxy using TATAMOTORS...")
            df = yf.download(
                "TATAMOTORS.NS",
                start=START_DATE,
                end=END_DATE,
                auto_adjust=False,
                multi_level_index=False,
                progress=False
            )

    if not df.empty:
        # Check if 'Adj Close' is in the flat columns; if not, fallback to 'Close'
        if 'Adj Close' in df.columns:
            adj_close = df[['Adj Close']].rename(columns={'Adj Close': 'Adj_Close'})
        elif 'Close' in df.columns:
            adj_close = df[['Close']].rename(columns={'Close': 'Adj_Close'})
        else:
            print(f"  ❌ No usable price columns found for {symbol}")
            continue

        adj_close.to_parquet(file_path)
        print(f"  ↳ Saved successfully. ({len(adj_close)} rows)")
    else:
        print(f"  ❌ Failed to retrieve any data for {symbol}")

print("\n--- Ingestion Complete! ---")