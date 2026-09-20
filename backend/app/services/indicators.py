from __future__ import annotations

import pandas as pd


def add_indicators(frame: pd.DataFrame, ema_period: int = 50, rsi_period: int = 14, atr_period: int = 14) -> pd.DataFrame:
    """Compute EMA/RSI/ATR/volume-average columns.

    `frame` must already be float64 (callers convert from the Decimal-typed
    Candle/CandleInput values before this point). pandas' .ewm()/.rolling()
    are float64-only under the hood regardless of input dtype -- feeding them
    an object-dtype column of Decimal silently downcasts to float and back
    with no error, which is worse than being explicit about the boundary:
    these are technical/statistical indicators, not settlement money, so
    float64 precision is the right tool here. Actual money math (entry/exit/
    stop/target/pnl) happens downstream in backtest.py using real Decimal.
    """
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Missing candle fields: {sorted(missing)}")
    result = frame.copy().sort_index()
    result["ema"] = result["close"].ewm(span=ema_period, adjust=False).mean()
    change = result["close"].diff()
    gain = change.clip(lower=0).rolling(rsi_period).mean()
    loss = -change.clip(upper=0).rolling(rsi_period).mean()
    result["rsi"] = 100 - (100 / (1 + gain.div(loss.replace(0, pd.NA))))
    previous_close = result["close"].shift(1)
    true_range = pd.concat([result["high"] - result["low"], (result["high"] - previous_close).abs(), (result["low"] - previous_close).abs()], axis=1).max(axis=1)
    result["atr"] = true_range.rolling(atr_period).mean()
    result["volume_avg"] = result["volume"].rolling(20).mean()
    return result


def signal_for_row(row: pd.Series, rules: dict) -> bool:
    if not bool(pd.notna(row[["ema", "rsi", "atr", "volume_avg"]]).all()):
        return False
    checks: list[bool] = []
    if rules.get("trend", True):
        checks.append(bool(row["close"] > row["ema"]))
    if rules.get("rsi", True):
        checks.append(bool(row["rsi"] > 50))
    if rules.get("volume", True):
        checks.append(bool(row["volume"] > row["volume_avg"]))
    return bool(checks) and all(checks)
