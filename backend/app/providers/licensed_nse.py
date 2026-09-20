from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ..config import get_settings

IST = timezone(timedelta(hours=5, minutes=30))

# Breeze's historical-data endpoint takes its own internal "stock_code", not the
# plain ticker. RELIANCE/INFY/HDFCBANK are verified against ICICI's own public
# NSEScripMaster.txt (ShortName column, downloaded from
# https://directlink.icicidirect.com/MotherAppMaster/SecurityMaster.zip). NIFTY
# 50 isn't in that file (it's an index, not a tradeable scrip); ("NIFTY", "NSE")
# was confirmed by a live sync returning close prices in the 23,000-24,000
# range, which only the index itself trades at.
SYMBOL_TO_STOCK_CODE: dict[str, tuple[str, str]] = {
    "RELIANCE": ("RELIND", "NSE"),
    "INFY": ("INFTEC", "NSE"),
    "HDFCBANK": ("HDFBAN", "NSE"),
    "NIFTY 50": ("NIFTY", "NSE"),
}

INTERVAL_MAP = {"1d": "1day", "1m": "1minute", "5m": "5minute", "30m": "30minute"}

_cached_client = None
_cached_client_key: tuple[str | None, str | None, str | None] | None = None


class LicensedNSEDataProvider:
    """Adapter to the ICICI Direct Breeze API (https://pypi.org/project/breeze-connect/).

    Refuses to run without real credentials and fails closed on any request or
    validation error -- it never falls back to scraping or synthetic data.

    breeze_connect is imported lazily inside `_client()`, not at module import
    time: the installed package does network I/O (downloading a ~3MB security
    master zip) and creates a `logs/` directory as a *side effect of import*,
    which is not something a web server's startup path should depend on.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.breeze_api_key
        self.api_secret = settings.breeze_api_secret
        self.session_token = settings.breeze_session_token

    def _client(self):
        global _cached_client, _cached_client_key
        cache_key = (self.api_key, self.api_secret, self.session_token)
        if _cached_client is None or _cached_client_key != cache_key:
            # breeze_connect calls urllib's urlopen() directly (not requests) for the
            # security-master/stock-script downloads it does on import and on every
            # generate_session(). That uses the interpreter's default SSL context, which
            # on some Python installs (notably python.org's macOS builds without their
            # postinstall cert script run) has no trusted root certificates at all and
            # fails every HTTPS request with CERTIFICATE_VERIFY_FAILED. Point it at
            # certifi's bundle instead -- this fixes verification, it doesn't weaken it.
            import os
            import certifi
            os.environ.setdefault("SSL_CERT_FILE", certifi.where())

            from breeze_connect import BreezeConnect  # noqa: PLC0415 -- see class docstring

            client = BreezeConnect(api_key=self.api_key)
            client.generate_session(api_secret=self.api_secret, session_token=self.session_token)
            _cached_client, _cached_client_key = client, cache_key
        return _cached_client

    def _fetch(self, interval: str, start: datetime, end: datetime, stock_code: str, exchange_code: str) -> dict:
        client = self._client()
        return client.get_historical_data_v2(
            interval=interval,
            from_date=start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            to_date=end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            stock_code=stock_code,
            exchange_code=exchange_code,
            product_type="cash",
        )

    async def candles(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[dict]:
        if not (self.api_key and self.api_secret and self.session_token):
            raise RuntimeError("Breeze provider is not configured (BREEZE_API_KEY / BREEZE_API_SECRET / BREEZE_SESSION_TOKEN)")
        if symbol not in SYMBOL_TO_STOCK_CODE:
            raise RuntimeError(f"No Breeze stock_code mapping for symbol {symbol!r}")
        interval = INTERVAL_MAP.get(timeframe)
        if not interval:
            raise RuntimeError(f"Unsupported timeframe for Breeze: {timeframe!r}")
        stock_code, exchange_code = SYMBOL_TO_STOCK_CODE[symbol]

        try:
            response = await asyncio.to_thread(self._fetch, interval, start, end, stock_code, exchange_code)
        except Exception as exc:
            # generate_session()/get_historical_data_v2() raise a bare Exception on
            # request-level failures (bad/expired session token, network error, ...);
            # validation errors instead come back as a {"Status": 500, "Error": ...} dict,
            # handled below. Both paths must fail closed the same way.
            raise RuntimeError(f"Breeze historical data request failed: {exc}") from exc

        if not isinstance(response, dict):
            raise RuntimeError("Breeze provider returned an unexpected response shape")
        if response.get("Error") or str(response.get("Status")) not in ("200", "0"):
            raise RuntimeError(f"Breeze API error: {response.get('Error') or response.get('Status')}")

        candles = []
        for row in response.get("Success") or []:
            naive = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S")
            as_utc = naive.replace(tzinfo=IST).astimezone(timezone.utc)
            candles.append({
                "timestamp": as_utc.isoformat(),
                # Breeze returns these as JSON strings (e.g. "1249.85"), so
                # Decimal(str) parses the exact printed value directly -- no
                # float round-trip to lose precision before it ever reaches
                # storage.
                "open": Decimal(str(row["open"])),
                "high": Decimal(str(row["high"])),
                "low": Decimal(str(row["low"])),
                "close": Decimal(str(row["close"])),
                "volume": Decimal(str(row["volume"])),
            })
        return candles
