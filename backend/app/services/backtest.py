from __future__ import annotations

from decimal import Decimal

import pandas as pd

from .indicators import add_indicators, signal_for_row


def _dec(value) -> Decimal:
    # str() first: Decimal(float) imports the float's binary representation
    # error (e.g. Decimal(1.1) == Decimal('1.100000000000000088817841970012...'));
    # Decimal(str(float)) does not.
    return Decimal(str(value))


def run_backtest(candles: pd.DataFrame, rules: dict, stop_atr: float = 1.5, target_r: float = 2.0) -> dict:
    if candles.empty:
        return {"status": "no_data", "trades": [], "metrics": {}}

    # Indicators run on float64 (pandas .ewm()/.rolling() are float64-only under
    # the hood regardless of input dtype -- see note in add_indicators). The
    # money math below -- entry/stop/target/exit/pnl -- is Decimal from this
    # point on, so P&L summed across many trades can't accumulate binary
    # floating-point rounding error.
    data = add_indicators(candles)
    stop_atr_d = _dec(stop_atr)
    target_r_d = _dec(target_r)

    trades: list[dict] = []
    position: dict | None = None
    for timestamp, row in data.iterrows():
        if position is None and signal_for_row(row, rules):
            risk = _dec(row["atr"]) * stop_atr_d
            if risk > 0:
                close = _dec(row["close"])
                position = {"entry_time": timestamp, "entry": close, "stop": close - risk, "target": close + risk * target_r_d}
        elif position is not None:
            exit_price = None
            reason = None
            if _dec(row["low"]) <= position["stop"]:
                exit_price, reason = position["stop"], "stop_loss"
            elif _dec(row["high"]) >= position["target"]:
                exit_price, reason = position["target"], "target"
            if exit_price is not None:
                trades.append({**position, "exit_time": timestamp, "exit": exit_price, "pnl": exit_price - position["entry"], "reason": reason})
                position = None

    pnls = [trade["pnl"] for trade in trades]
    if pnls:
        running = Decimal("0")
        peak = None
        max_drawdown = Decimal("0")
        for pnl in pnls:
            running += pnl
            peak = running if peak is None else max(peak, running)
            max_drawdown = min(max_drawdown, running - peak)
        win_rate = round(sum(1 for pnl in pnls if pnl > 0) / len(pnls) * 100, 2)
        net_pnl = round(float(sum(pnls, Decimal("0"))), 4)
        max_drawdown = round(float(max_drawdown), 4)
    else:
        win_rate = 0.0
        net_pnl = 0.0
        max_drawdown = 0.0

    return {
        "status": "complete",
        "trades": trades,
        "metrics": {
            "trade_count": len(trades),
            "win_rate": win_rate,
            "net_pnl": net_pnl,
            "max_drawdown": max_drawdown,
            "assumptions": {"direction": "long", "costs": "not included until configured", "fill_model": "next available candle close"},
        },
    }
