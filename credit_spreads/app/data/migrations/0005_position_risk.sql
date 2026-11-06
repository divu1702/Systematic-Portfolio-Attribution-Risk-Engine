-- 0005: planned risk of each position on the sizing basis (loss at the hard stop by default),
-- alongside max_loss, which is kept for the gap caps and the spec's expectancy metric.
ALTER TABLE trades ADD COLUMN risk REAL;
