-- The setup state machine's working values (sessions since Day 0, window position, Setup 2 route,
-- Day 0 ATR and volume filter, which books have entered), so a restart resumes exactly.
ALTER TABLE setups ADD COLUMN machine_json TEXT;
ALTER TABLE setups ADD COLUMN last_session DATE;
