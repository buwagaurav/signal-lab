"""In-process alert poller.

Runs on a fixed interval (ALERT_POLL_SECONDS) inside the FastAPI process
itself via APScheduler -- no separate worker process or broker to deploy.
That's the trade-off that comes with "in-process": it doesn't survive a
mid-poll restart, and running multiple API instances would mean multiple
independent pollers each doing the same work. Fine for one server; a durable
queue (Celery+Redis or similar) is the upgrade path if that stops being true.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal
from .models import Strategy
from .providers.licensed_nse import LicensedNSEDataProvider
from .services.alerts import MIN_CANDLES, AlertPolicy, LoggingNotificationChannel, deliver_pending, evaluate_strategy

logger = logging.getLogger("signallab.scheduler")

_scheduler: AsyncIOScheduler | None = None
_policy = AlertPolicy()
_channel = LoggingNotificationChannel()

# AlertPolicy.max_staleness_seconds (120s) is meant for intraday bars -- for
# every strategy in this app today (timeframe="1d"), the newest available
# closed candle is naturally hours old the moment it's published, so a
# literal 120s check would make the poller reject every daily strategy,
# always. Staleness tolerance has to scale with the bar's own duration;
# 3 days covers a normal weekend/holiday gap for daily bars.
STALENESS_OVERRIDE_SECONDS = {"1d": 3 * 24 * 60 * 60}


async def poll_alerts() -> None:
    """One poll cycle: evaluate every enabled strategy, then flush delivery.

    Fails closed per-strategy -- one strategy's provider error is logged and
    skipped, never crashes the whole cycle or blocks the others.
    """
    db = SessionLocal()
    try:
        strategies = db.scalars(select(Strategy).where(Strategy.enabled.is_(True))).all()
        if not strategies:
            return
        provider = LicensedNSEDataProvider()
        for strategy in strategies:
            try:
                await _poll_one(db, provider, strategy)
            except Exception:
                logger.exception("alert poll failed for strategy_id=%s", strategy.id)
        delivered = deliver_pending(db, _channel)
        if delivered:
            logger.info("delivered %d alert(s)", delivered)
    finally:
        db.close()


async def _poll_one(db, provider: LicensedNSEDataProvider, strategy: Strategy) -> None:
    timeframe = strategy.rules.get("timeframe", "1d")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=200)  # comfortably covers the 50-day EMA warmup
    candles = await provider.candles(strategy.symbol, timeframe, start, end)

    if len(candles) < MIN_CANDLES:
        logger.info("strategy_id=%s: only %d candles available, need %d -- skipping", strategy.id, len(candles), MIN_CANDLES)
        return

    if _policy.require_closed_candle:
        latest_ts = datetime.fromisoformat(str(candles[-1]["timestamp"]).replace("Z", "+00:00"))
        staleness = (end - latest_ts).total_seconds()
        limit = STALENESS_OVERRIDE_SECONDS.get(timeframe, _policy.max_staleness_seconds)
        if staleness > limit:
            logger.info("strategy_id=%s: latest candle is %.0fs old (limit %ss) -- skipping", strategy.id, staleness, limit)
            return

    frame = pd.DataFrame(candles).set_index("timestamp").sort_index()
    frame.index = pd.to_datetime(frame.index)
    frame = frame.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})

    result = evaluate_strategy(db, strategy, frame)
    if result.created:
        logger.info("strategy_id=%s: new alert, event_key=%s", strategy.id, result.event.event_key)


def start_scheduler() -> AsyncIOScheduler | None:
    global _scheduler
    settings = get_settings()
    if not settings.alerts_scheduler_enabled:
        logger.info("alert scheduler disabled (set ALERTS_SCHEDULER_ENABLED=true to enable)")
        return None
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(poll_alerts, "interval", seconds=settings.alert_poll_seconds, id="alert_poll", max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info("alert scheduler started, polling every %ss", settings.alert_poll_seconds)
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
