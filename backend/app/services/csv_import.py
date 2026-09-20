from __future__ import annotations

import io

import pandas as pd

ALIASES = {"symbol": ["symbol", "instrument", "tradingsymbol", "scrip"], "side": ["side", "transaction_type", "buy_sell", "type"], "quantity": ["quantity", "qty", "filled_quantity"], "entry_price": ["price", "average_price", "avg_price", "entry_price"], "broker_reference": ["order_id", "orderid", "trade_id"]}


def parse_broker_csv(raw: bytes) -> list[dict]:
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError("CSV is larger than the 10 MB safety limit")
    frame = pd.read_csv(io.BytesIO(raw))
    normalized = {str(col).strip().lower().replace(" ", "_"): col for col in frame.columns}
    selected: dict[str, str] = {}
    for target, aliases in ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                selected[target] = normalized[alias]
                break
    required = {"symbol", "side", "quantity", "entry_price"}
    if required - selected.keys():
        raise ValueError(f"CSV is missing required fields: {sorted(required - selected.keys())}")
    result = []
    for _, row in frame.iterrows():
        side = str(row[selected["side"]]).strip().upper()
        side = "LONG" if side in {"BUY", "B"} else "SHORT" if side in {"SELL", "S"} else side
        if side not in {"LONG", "SHORT"}:
            continue
        result.append({"symbol": str(row[selected["symbol"]]).strip().upper(), "side": side, "quantity": float(row[selected["quantity"]]), "entry_price": float(row[selected["entry_price"]]), "broker_reference": str(row[selected["broker_reference"]]) if "broker_reference" in selected else None})
    return result
