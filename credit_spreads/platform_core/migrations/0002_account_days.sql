-- 0002: each account's value at the end of every trading day, for today's P&L and the equity curves.
CREATE TABLE account_days (
    date            DATE NOT NULL,
    account         TEXT NOT NULL REFERENCES accounts(id),
    value           REAL NOT NULL,            -- cash balance minus the cost to close open positions
    cash_balance    REAL NOT NULL,
    used_margin     REAL NOT NULL,
    realised        REAL NOT NULL,
    unrealised      REAL NOT NULL,
    PRIMARY KEY (date, account)
);
