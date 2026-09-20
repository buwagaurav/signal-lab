from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime

import pandas as pd
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import AuditLogMiddleware
from .config import get_settings
from .db import Base, engine, get_db
from .models import Candle, Strategy, Trade, User
from .providers.licensed_nse import LicensedNSEDataProvider
from .rate_limit import RateLimitMiddleware
from .scheduler import start_scheduler, stop_scheduler
from .schemas import AuthRequest, CandleInput, PaperTradeCreate, StrategyCreate, StrategyResponse, TokenResponse, TradeResponse
from .security import current_user, hash_password, issue_token, verify_password
from .services.alerts import evaluate_strategy
from .services.csv_import import parse_broker_csv
from .services.backtest import run_backtest

# Python's root logger defaults to WARNING with no handler -- every logger.info()
# call in this codebase (scheduler status, the alert delivery channel's actual
# delivery mechanism) would otherwise be silently dropped, not just quiet.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = get_settings()
Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="SignalLab API", version="0.1.0", docs_url="/docs" if settings.app_env != "production" else None, lifespan=lifespan)
# Registration order matters: Starlette wraps outer-to-inner in reverse
# registration order, so the LAST middleware added is OUTERMOST. CORS must
# stay outermost so it can attach headers even to a 429 the rate limiter
# produces, or a response the audit logger never gets to see otherwise.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(AuditLogMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True, allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])
auth_scheme = HTTPBearer(auto_error=False)


def auth_user(credentials: HTTPAuthorizationCredentials | None = Depends(auth_scheme), db: Session = Depends(get_db)) -> User:
    return current_user(credentials, db)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "paper_mode": True, "licensed_data_configured": bool(settings.breeze_api_key and settings.breeze_api_secret and settings.breeze_session_token)}


@app.post("/auth/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: AuthRequest, db: Session = Depends(get_db)) -> TokenResponse:
    email = payload.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(email=email, password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return TokenResponse(access_token=issue_token(user))


@app.post("/auth/login", response_model=TokenResponse)
def login(payload: AuthRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return TokenResponse(access_token=issue_token(user))


@app.post("/strategies", response_model=StrategyResponse, status_code=status.HTTP_201_CREATED)
def create_strategy(payload: StrategyCreate, user: User = Depends(auth_user), db: Session = Depends(get_db)) -> Strategy:
    strategy = Strategy(user_id=user.id, name=payload.name, symbol=payload.symbol.upper(), rules=payload.rules, enabled=payload.enabled)
    db.add(strategy)
    db.commit()
    db.refresh(strategy)
    return strategy


@app.get("/strategies", response_model=list[StrategyResponse])
def list_strategies(user: User = Depends(auth_user), db: Session = Depends(get_db)) -> list[Strategy]:
    return list(db.scalars(select(Strategy).where(Strategy.user_id == user.id).order_by(Strategy.created_at.desc())))


@app.get("/trades", response_model=list[TradeResponse])
def list_trades(user: User = Depends(auth_user), db: Session = Depends(get_db)) -> list[Trade]:
    return list(db.scalars(select(Trade).where(Trade.user_id == user.id).order_by(Trade.opened_at.desc())))


@app.post("/trades/paper", response_model=TradeResponse, status_code=status.HTTP_201_CREATED)
def create_paper_trade(payload: PaperTradeCreate, user: User = Depends(auth_user), db: Session = Depends(get_db)) -> Trade:
    if payload.strategy_id and not db.scalar(select(Strategy).where(Strategy.id == payload.strategy_id, Strategy.user_id == user.id)):
        raise HTTPException(status_code=404, detail="Strategy not found")
    trade = Trade(user_id=user.id, status="PAPER", **payload.model_dump())
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return trade


@app.post("/trades/import-csv")
async def import_csv(file: UploadFile = File(...), user: User = Depends(auth_user), db: Session = Depends(get_db)) -> dict:
    if file.content_type not in {"text/csv", "application/csv", "application/vnd.ms-excel"}:
        raise HTTPException(status_code=415, detail="Only CSV files are accepted")
    try:
        rows = parse_broker_csv(await file.read())
    except (ValueError, pd.errors.ParserError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    for row in rows:
        db.add(Trade(user_id=user.id, status="IMPORTED", **row))
    db.commit()
    return {"imported": len(rows), "status": "quarantined_for_review", "message": "Imported trades are not treated as live orders."}


@app.post("/backtests/{strategy_id}")
def backtest(strategy_id: int, user: User = Depends(auth_user), db: Session = Depends(get_db)) -> dict:
    strategy = db.scalar(select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user.id))
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    rows = list(db.scalars(select(Candle).where(Candle.symbol == strategy.symbol, Candle.timeframe == strategy.rules.get("timeframe", "1d")).order_by(Candle.timestamp)))
    if not rows:
        raise HTTPException(status_code=422, detail="No licensed candles are stored for this strategy")
    frame = pd.DataFrame([{key: float(getattr(row, key)) for key in ("open", "high", "low", "close", "volume")} for row in rows], index=[row.timestamp for row in rows])
    return run_backtest(frame, strategy.rules, stop_atr=float(strategy.rules.get("stop_atr", 1.5)), target_r=float(strategy.rules.get("target_r", 2.0)))


@app.post("/alerts/evaluate/{strategy_id}")
def evaluate_paper_alert(strategy_id: int, candles: list[CandleInput], user: User = Depends(auth_user), db: Session = Depends(get_db)) -> dict:
    """Evaluate a closed-candle batch and create one idempotent paper alert.

    A production worker should call this after licensed data passes freshness checks.
    No order is ever placed by this endpoint.
    """
    strategy = db.scalar(select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user.id))
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    frame = pd.DataFrame([item.model_dump() for item in candles]).set_index("timestamp").sort_index()
    frame = frame.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
    try:
        result = evaluate_strategy(db, strategy, frame)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "triggered": result.triggered,
        "deduplicated": result.event is not None and not result.created,
        "mode": "PAPER",
        "event_id": result.event.id if result.event else None,
        "message": "No live order was created.",
    }


@app.post("/data/nse/sync")
async def sync_nse_data(symbol: str, timeframe: str, start: datetime, end: datetime, user: User = Depends(auth_user), db: Session = Depends(get_db)) -> dict:
    provider = LicensedNSEDataProvider()
    try:
        candles = await provider.candles(symbol.upper(), timeframe, start, end)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    stored = 0
    for item in candles:
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        if required - item.keys():
            raise HTTPException(status_code=502, detail="Licensed provider returned an incomplete candle")
        timestamp = datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00"))
        existing = db.scalar(select(Candle).where(Candle.symbol == symbol.upper(), Candle.timeframe == timeframe, Candle.timestamp == timestamp))
        values = {"symbol": symbol.upper(), "timeframe": timeframe, "timestamp": timestamp, "open": item["open"], "high": item["high"], "low": item["low"], "close": item["close"], "volume": item["volume"], "source": "licensed_provider"}
        if existing:
            for key, value in values.items():
                setattr(existing, key, value)
        else:
            db.add(Candle(**values))
        stored += 1
    db.commit()
    return {"symbol": symbol.upper(), "timeframe": timeframe, "received": len(candles), "stored": stored, "status": "persisted"}
