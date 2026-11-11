"""タイムマシン計算機：DB からデータを集めて計算し、画面に渡す形にまとめる。

UI（Streamlit）はここを呼ぶだけにする。将来 API 化してもそのまま使える。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Literal

import pandas as pd

from core.calc.dividends import estimate_dividends
from core.calc.returns import (
    Options,
    Order,
    SimResult,
    deposit,
    monthly_orders,
    simulate,
)
from core.data.jquants import normalize_code
from core.data.store import Store

#: 比較用の指数代替 ETF（Free プランでは指数そのものが取れないため）
BENCHMARKS: dict[str, str] = {
    "TOPIX（1306）": "13060",
    "日経平均（1321）": "13210",
}


@dataclass(frozen=True)
class Request:
    """タイムマシン計算の入力。

    mode が "lump" なら start に amount 円を一括投資、"dca" なら start〜end の毎月
    monthly_day 日に amount 円ずつ積立。
    """

    code: str
    start: date
    amount: int
    end: date | None = None
    mode: Literal["lump", "dca"] = "lump"
    monthly_day: int = 1
    opts: Options = field(default_factory=Options)
    deposit_rate: float = 0.002
    benchmarks: tuple[str, ...] = tuple(BENCHMARKS)


@dataclass
class Response:
    """計算結果。`chart` は (date, series, value) の縦長 DataFrame。"""

    main: SimResult
    benchmarks: dict[str, SimResult]
    deposit: pd.DataFrame
    chart: pd.DataFrame
    notes: list[str]


def _orders(req: Request, end: date) -> list[Order]:
    if req.mode == "lump":
        return [Order(req.start, req.amount)]
    return monthly_orders(req.start, end, req.amount, req.monthly_day)


def simulate_code(store: Store, code: str, req: Request, end: date | None = None) -> SimResult:
    """1 銘柄を計算する（配当は決算短信から推定）。"""
    code = normalize_code(code)
    bars = store.daily_bars(code, start=req.start, end=end or req.end)
    if bars.empty:
        raise ValueError(f"{code} の {req.start} 以降の株価データがありません")
    last = bars["date"].iloc[-1].date()
    end = end or req.end or last
    days = store.trading_days(req.start - timedelta(days=400), end + timedelta(days=120))
    divs = estimate_dividends(store.fin_summary_with_raw(code), days)
    return simulate(bars, _orders(req, end), divs, req.opts, end)


def run(store: Store, req: Request) -> Response:
    """メイン銘柄・指数代替 ETF・定期預金を同じ条件で計算する。"""
    main = simulate_code(store, req.code, req)
    end = main.end
    notes = list(main.notes)

    data_end = store.price_date_range()
    requested_end = req.end or (data_end[1] if data_end else end)
    if end < requested_end - timedelta(days=7):
        notes.append(
            f"株価データが {end} で終わっています（上場廃止・コード変更などの可能性）。"
            "評価額はその日の終値で計算しています"
        )

    # 指数代替：端数可・配当なし（分配金を含まない価格のみ）・非課税扱いで純粋な値動きを見る
    bench_opts = replace(req.opts, lot_size=None, fee_per_trade=0)
    benches: dict[str, SimResult] = {}
    for name in req.benchmarks:
        code = BENCHMARKS[name]
        bars = store.daily_bars(code, start=req.start, end=end)
        if bars.empty:
            notes.append(f"{name} の株価データがないため比較から外しました")
            continue
        benches[name] = simulate(bars, _orders(req, end), (), bench_opts, end)
    if benches:
        notes.append(
            "TOPIX・日経平均は連動 ETF（1306・1321）の価格で近似しています（分配金は含みません）"
        )

    dep = deposit(_orders(req, end), main.daily["date"].tolist(), req.deposit_rate)

    parts = [
        main.daily[["date", "value"]].assign(series="この銘柄"),
        *[r.daily[["date", "value"]].assign(series=n) for n, r in benches.items()],
        dep[["date", "value"]].assign(series=f"定期預金（年{req.deposit_rate:.2%}）"),
        main.daily[["date", "invested"]]
        .rename(columns={"invested": "value"})
        .assign(series="投資額（元本）"),
    ]
    chart = pd.concat(parts, ignore_index=True)
    chart["date"] = pd.to_datetime(chart["date"])
    return Response(main=main, benchmarks=benches, deposit=dep, chart=chart, notes=notes)


def compare(store: Store, codes: list[str], req: Request) -> dict[str, SimResult]:
    """複数銘柄（最大 5）を同じ条件で計算する。データのない銘柄は結果から除く。"""
    if len(codes) > 5:
        raise ValueError("比較できるのは最大 5 銘柄です")
    out: dict[str, SimResult] = {}
    for code in codes:
        try:
            out[normalize_code(code)] = simulate_code(store, code, req)
        except ValueError:
            continue
    return out
