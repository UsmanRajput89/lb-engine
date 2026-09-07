"""
REST wrapper for lb-engine's backtesting core.

Runs as a separate process from the MCP server (which stays on port 8000,
transport=streamable-http). This exposes the same backtest_service functions
used by the MCP tools (run_backtest / compare_strategies / walk_forward_backtest)
as plain JSON endpoints, so a non-MCP client (e.g. the future Laravel
lb-backend) can call them with ordinary HTTP.

No logic is duplicated here — every endpoint calls straight into
tradingview_mcp.core.services.backtest_service, the same module server.py's
MCP tools call. Add a new strategy there (see PROJECT_STATUS.md) and it is
automatically available through both interfaces.

Run:
    venv\\Scripts\\python.exe wrapper.py
    (or) venv\\Scripts\\uvicorn.exe wrapper:app --host 127.0.0.1 --port 8001
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

from tradingview_mcp.core.services.backtest_service import (
    run_backtest,
    compare_strategies,
    walk_forward_backtest,
)

app = FastAPI(title="lb-engine REST wrapper", version="0.1.0")


class BacktestRequest(BaseModel):
    symbol: str
    strategy: str
    period: str = "1y"
    initial_capital: float = 10_000.0
    commission_pct: float = 0.1
    slippage_pct: float = 0.05
    interval: str = "1d"
    include_trade_log: bool = False
    include_equity_curve: bool = False
    market: str = "stocks"


class CompareRequest(BaseModel):
    symbol: str
    period: str = "1y"
    initial_capital: float = 10_000.0
    commission_pct: float = 0.1
    slippage_pct: float = 0.05
    interval: str = "1d"
    market: str = "stocks"


class WalkForwardRequest(BaseModel):
    symbol: str
    strategy: str
    period: str = "2y"
    initial_capital: float = 10_000.0
    commission_pct: float = 0.1
    slippage_pct: float = 0.05
    n_splits: int = 3
    train_ratio: float = 0.7
    interval: str = "1d"
    market: str = "stocks"


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/backtest")
def backtest(req: BacktestRequest) -> dict:
    return run_backtest(
        req.symbol, req.strategy, req.period, req.initial_capital,
        req.commission_pct, req.slippage_pct, req.interval,
        req.include_trade_log, req.include_equity_curve, req.market,
    )


@app.post("/compare")
def compare(req: CompareRequest) -> dict:
    return compare_strategies(
        req.symbol, req.period, req.initial_capital,
        req.commission_pct, req.slippage_pct, req.interval, req.market,
    )


@app.post("/walk-forward")
def walk_forward(req: WalkForwardRequest) -> dict:
    return walk_forward_backtest(
        req.symbol, req.strategy, req.period, req.initial_capital,
        req.commission_pct, req.slippage_pct, req.n_splits,
        req.train_ratio, req.interval, req.market,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
