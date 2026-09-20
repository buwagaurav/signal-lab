import pandas as pd

from app.services.backtest import run_backtest


def test_backtest_returns_metrics():
    values = [100 + i * 0.5 for i in range(80)]
    frame = pd.DataFrame({"open": values, "high": [v + 1 for v in values], "low": [v - 1 for v in values], "close": values, "volume": [1000] * 80}, index=pd.date_range("2025-01-01", periods=80, freq="D"))
    result = run_backtest(frame, {"trend": True, "rsi": True, "volume": True})
    assert result["status"] == "complete"
    assert "trade_count" in result["metrics"]
