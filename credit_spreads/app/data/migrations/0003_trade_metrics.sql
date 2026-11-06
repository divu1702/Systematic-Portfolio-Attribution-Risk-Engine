-- 0003: fields the go-live metrics need on each trade (calibration, breakeven win rate,
-- IV/HV bucket, replayed exits) and the reason a mark was taken.
ALTER TABLE trades ADD COLUMN p_market     REAL;
ALTER TABLE trades ADD COLUMN p_model      REAL;
ALTER TABLE trades ADD COLUMN breakeven_wr REAL;
ALTER TABLE trades ADD COLUMN iv_hv        REAL;
ALTER TABLE trades ADD COLUMN short_k      REAL;
ALTER TABLE trades ADD COLUMN long_k       REAL;
ALTER TABLE trades ADD COLUMN replayed     BOOLEAN NOT NULL DEFAULT 0;  -- exit reconstructed from candles
ALTER TABLE marks  ADD COLUMN source       TEXT NOT NULL DEFAULT 'live'; -- live, replay
