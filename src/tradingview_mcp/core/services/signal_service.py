"""
Live signal state for backtest_service strategies.

A strategy is either flat or in a long position at any moment. This service
answers "what state is (strategy, symbol, interval) in right now?" using only
fully CLOSED candles, so the caller (lb-backend) can detect state transitions
(flat -> long = entry signal, long -> flat = exit signal) between evaluations.

Only crypto (Binance, 1h/4h/1d) and stocks (Yahoo, 1h/1d) are supported —
matching the long-only trend-following strategies that survived the cost-adjusted
scan, which only ever change state on a candle close.

`as_of` evaluates as if it were that past moment (candles that had not closed by
then are dropped). It exists so the whole signal pipeline can be tested by
replaying history instead of waiting days for a real trend flip.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from tradingview_mcp.core.services import backtest_service as _bs
from tradingview_mcp.core.services.market_data import _fetch_binance, _fetch_yahoo

_INTERVAL_MINUTES = {"1h": 60, "4h": 240, "1d": 1440}
_WARMUP_CANDLES = 400  # triple_ema needs 200+ for its SMA filter; the rest less
_MIN_CANDLES = 60


def _parse_candle_open(date_str: str) -> datetime:
    fmt = "%Y-%m-%d %H:%M" if " " in date_str else "%Y-%m-%d"
    return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)


def _parse_as_of(as_of: str) -> datetime:
    value = as_of.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get_signal_state(
    symbol: str,
    strategy: str,
    interval: str = "4h",
    market: str = "crypto",
    as_of: Optional[str] = None,
) -> dict:
    strategy = strategy.lower().strip()
    interval = interval.lower().strip()
    market = market.lower().strip()

    if strategy not in _bs._STRATEGY_MAP:
        return {"error": f"Unknown strategy '{strategy}'. Choose: {', '.join(_bs._STRATEGY_MAP)}"}
    if interval not in _INTERVAL_MINUTES:
        return {"error": f"Invalid interval '{interval}'. Choose: {', '.join(_INTERVAL_MINUTES)}"}
    if market not in ("crypto", "stocks"):
        return {"error": "Signals support market='crypto' or 'stocks' only."}
    if market == "stocks" and interval == "4h":
        return {"error": "Interval '4h' isn't available for stocks — use '1h' or '1d'."}

    try:
        cutoff = _parse_as_of(as_of) if as_of else datetime.now(timezone.utc)
    except ValueError:
        return {"error": "as_of must be an ISO datetime, e.g. 2026-09-01T08:00:00Z."}

    span_days = int(_WARMUP_CANDLES * _INTERVAL_MINUTES[interval] / 1440) + 3
    date_from = (cutoff - timedelta(days=span_days)).strftime("%Y-%m-%d")
    date_to = cutoff.strftime("%Y-%m-%d")

    try:
        if market == "crypto":
            # Binance for every interval (incl. 1d/1h) — same source the strategy
            # scan used; deliberately bypasses fetch_candles' Yahoo routing so
            # backtest behaviour is untouched.
            candles = _fetch_binance(symbol, "1y", interval, date_from, date_to)
        else:
            candles = _fetch_yahoo(symbol, "1y", interval, date_from, date_to)
    except Exception as e:
        return {"error": f"Failed to fetch data for '{symbol}': {e}"}

    step = timedelta(minutes=_INTERVAL_MINUTES[interval])
    closed = [c for c in candles if _parse_candle_open(c["date"]) + step <= cutoff]
    if len(closed) < _MIN_CANDLES:
        return {"error": f"Not enough closed candles ({len(closed)}) to evaluate '{strategy}'."}

    open_out: dict = {}
    trades = _bs._STRATEGY_MAP[strategy](closed, _open_out=open_out)
    position = open_out.get("position")
    last = closed[-1]
    last_trade = trades[-1] if trades else None

    return {
        "symbol":              symbol.upper(),
        "strategy":            strategy,
        "interval":            interval,
        "market":              market,
        "as_of":               cutoff.isoformat() if as_of else None,
        "evaluated_at":        datetime.now(timezone.utc).isoformat(),
        "candles_used":        len(closed),
        "last_closed_candle":  last["date"],
        "last_close":          last["close"],
        "position":            "long" if position else "flat",
        "open_position": (
            {"entry_date": position["entry_date"], "entry_price": position["entry_price"]}
            if position else None
        ),
        "last_exit": (
            {
                "entry_date":  last_trade["entry_date"],
                "entry_price": last_trade["entry_price"],
                "exit_date":   last_trade["exit_date"],
                "exit_price":  last_trade["exit_price"],
            }
            if last_trade else None
        ),
    }
