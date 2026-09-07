"""
Multi-source OHLCV candle fetching for backtest_service.py.

Yahoo Finance stays the default/fallback for everything (stocks, and
forex/crypto at 1d/1h — unchanged from before this module existed). Two
sources were added specifically to unlock the intraday history Yahoo can't
provide (it only keeps ~7 days of 1m and ~2 years of 1h data):

  - Binance (crypto, no API key — public market data endpoint)
  - OANDA   (forex, needs a free practice-account personal access token —
             set OANDA_API_KEY in .env; without it, forex intraday requests
             fail with a clear configuration error rather than a confusing
             network error)

Routing (see fetch_candles):
  market="stocks" (default) -> always Yahoo; intraday (<1h) is rejected
                                with a clear "not supported yet" error —
                                stocks intraday needs a paid data provider,
                                out of scope for now.
  market="crypto"           -> Yahoo for 1d/1h (unchanged), Binance for
                                1m/5m/15m/30m/4h.
  market="forex"            -> Yahoo for 1d/1h (unchanged), OANDA for
                                1m/5m/15m/30m/4h.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

# Load .env eagerly here too — wrapper.py imports backtest_service (which
# imports this module) without ever importing yahoo_finance_service, the
# only other place that currently triggers this load. Without this,
# OANDA_API_KEY set in lb-engine/.env would be invisible to the wrapper.
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(__file__), "../../../../.env")
    load_dotenv(dotenv_path=_env_path, override=False)
except ImportError:
    pass

_UA = "tradingview-mcp/0.7.0 backtest-bot"

# Interval sets — Yahoo only ever supported these two; the rest are only
# reachable via Binance/OANDA (see ROUTING table below).
_YAHOO_INTERVALS = {"1d", "1h"}
_INTRADAY_INTERVALS = {"1m", "5m", "15m", "30m", "4h"}
VALID_INTERVALS = _YAHOO_INTERVALS | _INTRADAY_INTERVALS

VALID_MARKETS = {"stocks", "forex", "crypto"}

_PERIOD_TO_DAYS = {
    "1mo": 30,
    "3mo": 90,
    "6mo": 180,
    "1y": 365,
    "2y": 730,
}

# Hard cap on paginated API calls per fetch — protects against a request
# that would otherwise take many minutes (or longer) to assemble, e.g. a
# very long period at very fine granularity. Trips well before that.
_MAX_PAGES = 1500


def validate_market_and_interval(market: str, interval: str) -> Optional[str]:
    """Return an error message if the market/interval combo isn't supported, else None."""
    if market not in VALID_MARKETS:
        return f"Unknown market '{market}'. Choose: {', '.join(sorted(VALID_MARKETS))}"
    if interval not in VALID_INTERVALS:
        return f"Invalid interval '{interval}'. Choose: {', '.join(sorted(VALID_INTERVALS))}"
    if market == "stocks" and interval in _INTRADAY_INTERVALS:
        return (
            f"Intraday interval '{interval}' isn't available for stocks yet "
            f"(would need a paid data provider) — use '1d' or '1h'."
        )
    return None


def fetch_candles(symbol: str, period: str, interval: str, market: str = "stocks") -> list[dict]:
    """Single entry point backtest_service.py calls instead of hitting Yahoo directly."""
    if market in ("stocks", "forex", "crypto") and interval in _YAHOO_INTERVALS:
        return _fetch_yahoo(symbol, period, interval)
    if market == "crypto":
        return _fetch_binance(symbol, period, interval)
    if market == "forex":
        return _fetch_oanda(symbol, period, interval)
    # market == "stocks" + intraday interval is already rejected by
    # validate_market_and_interval() before this is ever reached.
    raise ValueError(f"No data route for market={market!r} interval={interval!r}")


# ─── Yahoo Finance (existing behavior, moved here unchanged) ──────────────────

_YF_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"


def _fetch_yahoo(symbol: str, period: str, interval: str = "1d") -> list[dict]:
    url = f"{_YF_BASE}/{symbol}?interval={interval}&range={period}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})

    data = None
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass

    if data is None:
        try:
            from tradingview_mcp.core.services.proxy_manager import build_opener_with_proxy
            opener = build_opener_with_proxy(_UA)
            with opener.open(url, timeout=18) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"Both direct and proxy connections failed: {e}")

    result = data["chart"]["result"][0]
    timestamps = result["timestamp"]
    q = result["indicators"]["quote"][0]
    date_fmt = "%Y-%m-%d %H:%M" if interval == "1h" else "%Y-%m-%d"

    candles = []
    for i, ts in enumerate(timestamps):
        o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        if None in (o, h, l, c):
            continue
        candles.append({
            "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime(date_fmt),
            "open": round(o, 4),
            "high": round(h, 4),
            "low": round(l, 4),
            "close": round(c, 4),
            "volume": v or 0,
        })
    return candles


# ─── Binance (crypto intraday) ────────────────────────────────────────────────

_BINANCE_BASE = "https://api.binance.com/api/v3/klines"
_BINANCE_LIMIT = 1000  # max candles per call


def _normalize_binance_symbol(symbol: str) -> str:
    """Accepts today's Yahoo-style 'BTC-USD' or an already-Binance 'BTCUSDT'."""
    symbol = symbol.strip().upper()
    if "-" in symbol:
        base, quote = symbol.split("-", 1)
        if quote == "USD":
            quote = "USDT"  # Binance's actual liquid USD-pegged pair
        return f"{base}{quote}"
    return symbol


def _fetch_binance(symbol: str, period: str, interval: str) -> list[dict]:
    binance_symbol = _normalize_binance_symbol(symbol)
    days = _PERIOD_TO_DAYS.get(period, 365)
    end_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000

    candles: list[dict] = []
    cursor = start_ms
    date_fmt = "%Y-%m-%d" if interval == "1d" else "%Y-%m-%d %H:%M"

    for _ in range(_MAX_PAGES):
        params = urllib.parse.urlencode({
            "symbol": binance_symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": _BINANCE_LIMIT,
        })
        req = urllib.request.Request(f"{_BINANCE_BASE}?{params}", headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                batch = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"Binance request failed for '{binance_symbol}': {e}")

        if isinstance(batch, dict) and batch.get("code"):
            raise RuntimeError(f"Binance error for '{binance_symbol}': {batch.get('msg', batch)}")
        if not batch:
            break

        for row in batch:
            open_time_ms, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
            candles.append({
                "date": datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).strftime(date_fmt),
                "open": round(float(o), 8),
                "high": round(float(h), 8),
                "low": round(float(l), 8),
                "close": round(float(c), 8),
                "volume": float(v),
            })

        last_open_time = batch[-1][0]
        if last_open_time <= cursor or len(batch) < _BINANCE_LIMIT:
            break
        cursor = last_open_time + 1

        if cursor >= end_ms:
            break
    else:
        raise RuntimeError(
            f"Date range too large for interval '{interval}' (hit the {_MAX_PAGES}-request "
            f"pagination cap) — try a shorter period."
        )

    return candles


# ─── OANDA (forex intraday) ───────────────────────────────────────────────────

_OANDA_GRANULARITY = {
    "1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30", "1h": "H1", "4h": "H4", "1d": "D",
}
_OANDA_LIMIT = 5000  # max candles per call


def _normalize_oanda_symbol(symbol: str) -> str:
    """Accepts today's Yahoo-style 'EURUSD=X' or an already-OANDA 'EUR_USD'."""
    symbol = symbol.strip().upper()
    if symbol.endswith("=X"):
        symbol = symbol[:-2]
    if "_" not in symbol and len(symbol) == 6:
        symbol = f"{symbol[:3]}_{symbol[3:]}"
    return symbol


def _oanda_base_url() -> str:
    env = os.environ.get("OANDA_ENVIRONMENT", "practice").strip().lower()
    return "https://api-fxpractice.oanda.com" if env != "live" else "https://api-fxtrade.oanda.com"


def _fetch_oanda(symbol: str, period: str, interval: str) -> list[dict]:
    api_key = os.environ.get("OANDA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OANDA_API_KEY is not configured — set it in lb-engine/.env "
            "(see docs/trading-tool-tech-decisions.md for how to get a free practice-account token)."
        )

    instrument = _normalize_oanda_symbol(symbol)
    granularity = _OANDA_GRANULARITY[interval]
    days = _PERIOD_TO_DAYS.get(period, 365)
    end_dt = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=days)

    candles: list[dict] = []
    cursor = start_dt
    date_fmt = "%Y-%m-%d" if interval == "1d" else "%Y-%m-%d %H:%M"
    base_url = _oanda_base_url()

    for _ in range(_MAX_PAGES):
        params = urllib.parse.urlencode({
            "granularity": granularity,
            "price": "M",  # midpoint prices
            "from": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "count": _OANDA_LIMIT,
        })
        url = f"{base_url}/v3/instruments/{instrument}/candles?{params}"
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Authorization": f"Bearer {api_key}",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"OANDA request failed for '{instrument}': {e}")

        batch = data.get("candles", [])
        if not batch:
            break

        last_time = None
        for row in batch:
            if not row.get("complete", True):
                continue
            mid = row["mid"]
            row_time = datetime.strptime(row["time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            last_time = row_time
            candles.append({
                "date": row_time.strftime(date_fmt),
                "open": round(float(mid["o"]), 6),
                "high": round(float(mid["h"]), 6),
                "low": round(float(mid["l"]), 6),
                "close": round(float(mid["c"]), 6),
                "volume": row.get("volume", 0),
            })

        if last_time is None or last_time <= cursor or len(batch) < _OANDA_LIMIT:
            break
        cursor = last_time + timedelta(seconds=1)

        if cursor >= end_dt:
            break
    else:
        raise RuntimeError(
            f"Date range too large for interval '{interval}' (hit the {_MAX_PAGES}-request "
            f"pagination cap) — try a shorter period."
        )

    return candles
