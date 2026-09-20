"""Seed synthetic OHLCV candles for local backtesting.

No licensed NSE vendor is configured for local development (LICENSED_NSE_DATA_URL
is unset), so /backtests has nothing to run against and /data/nse/sync fails
closed by design. This inserts clearly-labeled synthetic candles (source=
"seed_demo") for the four symbols the demo UI offers, so the signal validator
can exercise the real indicator and backtest engine end to end. These are NOT
real market prices.

Usage (from backend/, with the virtualenv active):
    python -m app.seed_demo_data
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from .db import Base, SessionLocal, engine
from .models import Candle

TIMEFRAME = "1d"
TRADING_DAYS = 800

# start price and per-step return distribution are arbitrary synthetic values,
# not real quotes -- only the relative differences give the validator varied
# hit rates to compare across instruments.
SYMBOLS = {
    "RELIANCE": {"start": 1000.0, "drift": 0.0006, "vol": 0.014, "seed": 1},
    "INFY": {"start": 700.0, "drift": 0.0003, "vol": 0.016, "seed": 2},
    "HDFCBANK": {"start": 850.0, "drift": 0.0002, "vol": 0.013, "seed": 3},
    "NIFTY 50": {"start": 1200.0, "drift": 0.0005, "vol": 0.010, "seed": 4},
}


def _generate(params: dict) -> list[dict]:
    rng = random.Random(params["seed"])
    price = params["start"]
    day = datetime.now(timezone.utc) - timedelta(days=int(TRADING_DAYS * 1.45))
    candles: list[dict] = []
    while len(candles) < TRADING_DAYS:
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        ret = rng.gauss(params["drift"], params["vol"])
        open_price = price
        close_price = max(0.5, open_price * (1 + ret))
        wick = abs(rng.gauss(0, params["vol"])) * open_price
        high = max(open_price, close_price) + wick * 0.4
        low = max(0.1, min(open_price, close_price) - wick * 0.4)
        volume = (1_000_000 + rng.random() * 400_000) * (1.6 if rng.random() < 0.15 else 1.0)
        candles.append({
            "timestamp": day.replace(hour=9, minute=15, second=0, microsecond=0),
            "open": round(open_price, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close_price, 2),
            "volume": round(volume, 2),
        })
        price = close_price
    return candles


def seed() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    total_inserted = 0
    try:
        for symbol, params in SYMBOLS.items():
            existing = db.query(Candle).filter(Candle.symbol == symbol, Candle.timeframe == TIMEFRAME).count()
            if existing >= TRADING_DAYS:
                print(f"{symbol}: already seeded ({existing} candles), skipping")
                continue
            rows = _generate(params)
            for row in rows:
                db.add(Candle(symbol=symbol, timeframe=TIMEFRAME, source="seed_demo", **row))
            db.commit()
            total_inserted += len(rows)
            print(f"{symbol}: inserted {len(rows)} synthetic candles")
    finally:
        db.close()
    print(f"Done. {total_inserted} synthetic candles inserted. These are NOT real market prices.")


if __name__ == "__main__":
    seed()
