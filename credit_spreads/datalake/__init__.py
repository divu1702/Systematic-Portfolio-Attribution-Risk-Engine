"""The market-data repository for Indian stock futures and options (platform.yaml `lake`).

Layout under `lake.root` (Parquet with zstd compression; hive partitions; times are IST, naive):

  options_1m/underlying=<SYM>/expiry=<YYYY-MM-DD>/*.parquet   Kite 1-minute candles with OI, every listed
                                                               strike of the archived series
  futures_1m/expiry=<YYYY-MM-DD>/*.parquet                     Kite 1-minute candles with OI, all stock futures
  equity_1m/month=<YYYY-MM>/*.parquet                          1-minute candles of the F&O stocks and indices
  quotes_1m/date=<YYYY-MM-DD>/*.parquet                        live quotes once a minute from Kite's WebSocket:
                                                               bid, ask, sizes, orders, last, volume, OI, totals
  instruments/date=<YYYY-MM-DD>/*.parquet                      the day's instrument master (tokens, lots, strikes)
  bhav_fo/date=<YYYY-MM-DD>/*.parquet, bhav_cm/...             NSE bhavcopies, parsed (every contract, every day)
  nse_raw/<kind>/<YYYY>/<file>                                 every NSE file exactly as downloaded
  catalog.db                                                   what is stored, progress, gaps, runs

New data arrives as `part-*.parquet` files; `compact` merges a partition into one `data.parquet`,
keeping the newest copy of each row. Partition values live in the directory names only.
Query with `datalake.api.Lake(root)` (DuckDB) or any Parquet reader.
"""
