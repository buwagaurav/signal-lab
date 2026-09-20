"""Alert evaluation and delivery.

Shared by the HTTP route (/alerts/evaluate/{id}, caller supplies candles) and
the scheduled poller (app/scheduler.py, pulls candles from the licensed
provider itself) so both paths run through exactly the same dedup logic --
there is only one place an AlertEvent gets created.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AlertEvent, Strategy
from .indicators import add_indicators, signal_for_row

logger = logging.getLogger("signallab.alerts")

MIN_CANDLES = 55


@dataclass(frozen=True)
class AlertPolicy:
    max_staleness_seconds: int = 120
    require_closed_candle: bool = True
    paper_only: bool = True


@dataclass
class AlertResult:
    triggered: bool
    event: AlertEvent | None
    created: bool  # True only if this call created the event, vs. found an existing one


def evaluate_strategy(db: Session, strategy: Strategy, frame: pd.DataFrame) -> AlertResult:
    """Evaluate a closed-candle frame against one strategy and create at most
    one idempotent AlertEvent for it.

    `frame` must be indexed by timestamp (ascending), float64 OHLCV columns,
    at least MIN_CANDLES rows -- callers convert from Decimal before this point
    (see add_indicators' docstring for why).
    """
    if len(frame) < MIN_CANDLES:
        raise ValueError(f"At least {MIN_CANDLES} closed candles are required")

    enriched = add_indicators(frame)
    latest = enriched.iloc[-1]
    triggered = signal_for_row(latest, strategy.rules)
    event_key = f"{strategy.id}:{strategy.version}:{enriched.index[-1].isoformat()}"

    event = db.scalar(select(AlertEvent).where(AlertEvent.event_key == event_key))
    created = False
    if triggered and not event:
        event = AlertEvent(
            strategy_id=strategy.id,
            candle_timestamp=enriched.index[-1],
            event_key=event_key,
            payload={
                "symbol": strategy.symbol,
                "close": float(latest["close"]),
                "ema": float(latest["ema"]),
                "rsi": float(latest["rsi"]),
                "atr": float(latest["atr"]),
                "mode": "PAPER",
            },
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        created = True

    return AlertResult(triggered=triggered, event=event, created=created)


class NotificationChannel:
    """Delivery boundary for triggered alerts. deliver() returns True on
    successful delivery; the caller only flips AlertEvent.delivered on a
    True return, so a failed delivery stays eligible for retry.
    """

    def deliver(self, event: AlertEvent) -> bool:
        raise NotImplementedError


class LoggingNotificationChannel(NotificationChannel):
    """Default channel: structured log line, nothing else. No delivery
    credentials (SMTP, Slack webhook, ...) have been configured or asked for,
    so this is the only channel that's honest to ship without inventing an
    integration nobody set up. Swap in a real channel later behind the same
    deliver(event) -> bool interface.
    """

    def deliver(self, event: AlertEvent) -> bool:
        logger.info(
            "ALERT strategy_id=%s symbol=%s close=%s rsi=%s event_key=%s (paper mode -- no order placed)",
            event.strategy_id, event.payload.get("symbol"), event.payload.get("close"),
            event.payload.get("rsi"), event.event_key,
        )
        return True


def deliver_pending(db: Session, channel: NotificationChannel) -> int:
    """Deliver every AlertEvent not yet marked delivered.

    event_key is the create-time idempotency key (enforced by evaluate_strategy
    above); delivered is the delivery-time one -- an event is only ever handed
    to the channel while delivered is still False, and flips to True in the
    same pass it succeeds in, so a re-run only retries events that actually
    failed last time.
    """
    pending = db.scalars(select(AlertEvent).where(AlertEvent.delivered.is_(False))).all()
    delivered_count = 0
    for event in pending:
        if channel.deliver(event):
            event.delivered = True
            delivered_count += 1
    if delivered_count:
        db.commit()
    return delivered_count
