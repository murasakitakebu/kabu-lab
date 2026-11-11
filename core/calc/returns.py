"""一括投資・積立投資のリターン計算（タイムマシン計算機のエンジン）。

調整後価格ではなく**未調整の終値と実際の株数**で日々の保有を追う。
- 株式分割・併合：J-Quants の AdjFactor（例: 1:2 分割なら 0.5）が付いた日に株数を 1/AdjFactor 倍。
  併合などで端数が出たら、端数分はその日の終値で現金化する（端数株式の売却代金の近似）。
- 配当：権利落ち日の前日終了時点の株数 × 1株配当。支払日に税引き後の額を受け取る。
  再投資ありなら支払日の終値で買い増し、なしなら現金で持つ（利息なし）。
- 税金：配当は受取時に源泉徴収。評価額は「税引前」と「その日に全部売った場合の税引後」を両方出す。
- 金額は円の整数／Decimal で扱う。株価は Decimal に変換してから掛け算する。
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import pandas as pd

from core.calc.tax import tax_on_income

#: 端数株（lot_size=None のとき）の株数の精度
_FRACTIONAL_Q = Decimal("0.000001")


@dataclass(frozen=True)
class Order:
    """買い注文：`date` 以降の最初の取引日の終値で `amount` 円分買う。"""

    date: date
    amount: int


@dataclass(frozen=True)
class Dividend:
    """1株あたりの配当。`estimated` は Free プランでの推定値かどうか。"""

    ex_date: date
    pay_date: date
    amount_per_share: Decimal
    estimated: bool = False


@dataclass(frozen=True)
class Options:
    """計算条件。

    Attributes:
        lot_size: 売買単位（100=単元株、1=1株単位、None=端数可。指数の近似などに使う）
        reinvest_dividends: 配当を再投資するか
        nisa: NISA 口座（配当・譲渡益とも非課税）
        fee_per_trade: 1 回の売買ごとの手数料（円）
    """

    lot_size: int | None = 1
    reinvest_dividends: bool = True
    nisa: bool = False
    fee_per_trade: int = 0


@dataclass
class SimResult:
    """計算結果。`daily` は日次の推移（チャート用）。"""

    shares: Decimal
    invested: int  # 拠出額の累計
    cost_basis: int  # 取得価額（手数料込み）
    cash: int  # 未投資の現金（再投資待ち・端数・再投資なしの配当を含む）
    market_value: int  # 株式の時価
    total_value: int  # 時価＋現金＋未収配当（税引前・売却前）
    after_tax_value: int  # 最終日に全部売った場合の手取り
    dividends_gross: int
    dividends_tax: int
    dividends_receivable: int  # 権利確定済み・未払いの配当（税引後）
    fees: int
    profit: int  # total_value - invested
    total_return: float  # profit / invested
    after_tax_return: float
    irr: float | None  # 年率（金額加重。一括投資なら CAGR と一致）
    max_drawdown: float  # 時間加重リターンの最大下落率（負の値）
    start: date
    end: date
    daily: pd.DataFrame = field(repr=False)
    notes: list[str] = field(default_factory=list)


def _d(x: object) -> Decimal:
    return Decimal(str(x))


def _ceil_yen(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_CEILING))


def _floor_yen(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_FLOOR))


class _Portfolio:
    def __init__(self, opts: Options) -> None:
        self.opts = opts
        self.shares = Decimal(0)
        self.cash = 0  # 投資に回す現金
        self.idle_cash = 0  # 再投資しない配当
        self.cost_basis = 0
        self.fees = 0

    def buy(self, price: Decimal) -> None:
        """手元の投資用現金で買えるだけ買う。"""
        fee = self.opts.fee_per_trade
        budget = self.cash - fee
        if budget <= 0 or price <= 0:
            return
        lot = self.opts.lot_size
        if lot is None:
            n = (Decimal(budget) / price).quantize(_FRACTIONAL_Q, rounding=ROUND_FLOOR)
            cost = budget if n > 0 else 0
        else:
            n = Decimal(int(Decimal(budget) / price / lot) * lot)
            cost = _ceil_yen(n * price)
            while n > 0 and cost > budget:  # 切り上げで予算を超える場合
                n -= lot
                cost = _ceil_yen(n * price)
        if n <= 0:
            return
        self.shares += n
        self.cash -= cost + fee
        self.cost_basis += cost + fee
        self.fees += fee

    def split(self, factor: Decimal, price: Decimal) -> None:
        """分割・併合（factor = AdjFactor）。端数は終値で現金化。"""
        if self.shares == 0 or factor == 1 or factor <= 0:
            return
        new = self.shares / factor
        if self.opts.lot_size is not None:
            whole = new.to_integral_value(rounding=ROUND_FLOOR)
            frac = new - whole
            if frac > 0:
                self.cash += _floor_yen(frac * price)
            new = whole
        self.shares = new

    def liquidation(self, price: Decimal) -> tuple[int, int]:
        """全部売った場合の (売却代金, 譲渡益税)。"""
        if self.shares == 0:
            return 0, 0
        proceeds = _floor_yen(self.shares * price) - self.opts.fee_per_trade
        tax = tax_on_income(proceeds - self.cost_basis, nisa=self.opts.nisa)
        return proceeds, tax


def simulate(
    bars: pd.DataFrame,
    orders: Iterable[Order],
    dividends: Iterable[Dividend] = (),
    opts: Options | None = None,
    end: date | None = None,
) -> SimResult:
    """売買・分割・配当を日々たどって評価額を計算する。

    Args:
        bars: 1 銘柄の日足。列 `date`, `close`（未調整終値）, `adj_factor`。
        orders: 買い注文（一括なら 1 件、積立なら毎回分）。
        dividends: 1株配当のリスト。
        opts: 計算条件。
        end: 評価日（省略時は bars の最終日）。
    """
    opts = opts or Options()
    orders = sorted(orders, key=lambda o: o.date)
    if not orders:
        raise ValueError("orders が空です")
    divs = sorted(dividends, key=lambda d: d.ex_date)

    df = bars[["date", "close", "adj_factor"]].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.dropna(subset=["close"]).sort_values("date")
    df = df[df["date"] >= orders[0].date]
    if end is not None:
        df = df[df["date"] <= end]
    if df.empty:
        raise ValueError("購入日以降の株価データがありません")

    pf = _Portfolio(opts)
    invested = 0
    div_gross = div_tax = 0
    pending: list[tuple[date, int, int]] = []  # (支払日, 税引前, 税額)
    oi = di = 0
    prev_day: date | None = None
    prev_value = 0
    twr_index = 1.0
    flows: list[tuple[date, int]] = []  # IRR 用（拠出はマイナス）
    rows = []

    for day, close, factor in df.itertuples(index=False):
        price = _d(close)
        # 1) 配当の権利：権利落ち日が (前日, 今日] にあれば、前日終了時点の株数で確定
        while di < len(divs) and divs[di].ex_date <= day:
            dv = divs[di]
            if prev_day is not None and dv.ex_date > prev_day and pf.shares > 0:
                gross = _floor_yen(pf.shares * dv.amount_per_share)
                tax = tax_on_income(gross, nisa=opts.nisa)
                pending.append((dv.pay_date, gross, tax))
                div_gross += gross
                div_tax += tax
            di += 1
        # 2) 分割・併合
        if factor is not None and not pd.isna(factor):
            pf.split(_d(factor), price)
        # 3) 配当の受け取り
        still: list[tuple[date, int, int]] = []
        for pay, gross, tax in pending:
            if pay <= day:
                if opts.reinvest_dividends:
                    pf.cash += gross - tax
                else:
                    pf.idle_cash += gross - tax
            else:
                still.append((pay, gross, tax))
        pending = still
        # 4) 注文（当日以前で未執行のもの）
        flow = 0
        while oi < len(orders) and orders[oi].date <= day:
            pf.cash += orders[oi].amount
            invested += orders[oi].amount
            flow += orders[oi].amount
            oi += 1
        if flow:
            flows.append((day, -flow))
        if flow or (opts.reinvest_dividends and pf.cash > 0):
            pf.buy(price)
        # 5) 評価
        market = _floor_yen(pf.shares * price)
        receivable = sum(g - t for _, g, t in pending)
        value = market + pf.cash + pf.idle_cash + receivable
        if prev_value > 0:
            twr_index *= (value - flow) / prev_value
        rows.append(
            {
                "date": day,
                "close": float(price),
                "shares": float(pf.shares),
                "invested": invested,
                "market_value": market,
                "cash": pf.cash + pf.idle_cash,
                "value": value,
                "twr_index": twr_index,
            }
        )
        prev_value = value
        prev_day = day

    daily = pd.DataFrame(rows)
    last_price = _d(df["close"].iloc[-1])
    last_day = daily["date"].iloc[-1]
    receivable = sum(g - t for _, g, t in pending)
    market = _floor_yen(pf.shares * last_price)
    total = market + pf.cash + pf.idle_cash + receivable
    proceeds, cg_tax = pf.liquidation(last_price)
    after_tax = proceeds - cg_tax + pf.cash + pf.idle_cash + receivable

    peak = daily["twr_index"].cummax()
    mdd = float((daily["twr_index"] / peak - 1).min())
    irr = xirr([*flows, (last_day, total)])

    notes = []
    if oi < len(orders):
        notes.append(f"評価日以降の注文 {len(orders) - oi} 件は計算に含めていません")
    if any(d.estimated for d in divs):
        notes.append("配当は決算短信の1株配当から推定した値です（権利落ち日・支払日は近似）")
    if pending:
        notes.append("権利確定済みで未払いの配当を評価額に含めています")

    return SimResult(
        shares=pf.shares,
        invested=invested,
        cost_basis=pf.cost_basis,
        cash=pf.cash + pf.idle_cash,
        market_value=market,
        total_value=total,
        after_tax_value=after_tax,
        dividends_gross=div_gross,
        dividends_tax=div_tax,
        dividends_receivable=receivable,
        fees=pf.fees,
        profit=total - invested,
        total_return=(total - invested) / invested if invested else 0.0,
        after_tax_return=(after_tax - invested) / invested if invested else 0.0,
        irr=irr,
        max_drawdown=mdd,
        start=daily["date"].iloc[0],
        end=last_day,
        daily=daily,
        notes=notes,
    )


def lump_sum(
    bars: pd.DataFrame,
    buy_date: date,
    amount: int,
    dividends: Iterable[Dividend] = (),
    opts: Options | None = None,
    end: date | None = None,
) -> SimResult:
    """一括投資：`buy_date` に `amount` 円分買って `end` まで持ち続けた場合。"""
    return simulate(bars, [Order(buy_date, amount)], dividends, opts, end)


def monthly_orders(start: date, end: date, amount: int, day: int = 1) -> list[Order]:
    """積立の注文：`start`〜`end` の毎月 `day` 日（休日なら次の取引日に執行される）。"""
    orders = []
    y, m = start.year, start.month
    while True:
        last = pd.Timestamp(y, m, 1).days_in_month
        d = date(y, m, min(day, last))
        if d > end:
            break
        if d >= start:
            orders.append(Order(d, amount))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return orders


def dollar_cost_averaging(
    bars: pd.DataFrame,
    start: date,
    end: date,
    monthly_amount: int,
    dividends: Iterable[Dividend] = (),
    opts: Options | None = None,
    day: int = 1,
) -> SimResult:
    """積立投資：毎月 `monthly_amount` 円ずつ買い続けた場合（ドルコスト平均法）。"""
    return simulate(bars, monthly_orders(start, end, monthly_amount, day), dividends, opts, end)


def xirr(flows: list[tuple[date, int | float]]) -> float | None:
    """不定期キャッシュフローの内部収益率（年率）。拠出はマイナス、受取はプラス。"""
    flows = [(d, float(v)) for d, v in flows if v]
    if len(flows) < 2 or all(v >= 0 for _, v in flows) or all(v <= 0 for _, v in flows):
        return None
    t0 = min(d for d, _ in flows)

    def npv(rate: float) -> float:
        return sum(v / (1 + rate) ** ((d - t0).days / 365.0) for d, v in flows)

    lo, hi = -0.9999, 1.0
    while npv(hi) > 0 and hi < 1e6:
        hi *= 2
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return (lo + hi) / 2


def deposit(orders: Iterable[Order], dates: Iterable[date], annual_rate: float) -> pd.DataFrame:
    """定期預金と比較するための推移。年利 `annual_rate` で日割り複利、利子に 20.315% 課税。

    戻り値の列：date, invested, value（税引前）, after_tax_value
    """
    orders = sorted(orders, key=lambda o: o.date)
    rows = []
    for day in dates:
        invested = 0
        value = 0.0
        for o in orders:
            if o.date > day:
                break
            invested += o.amount
            value += o.amount * (1 + annual_rate) ** ((day - o.date).days / 365.0)
        value_yen = math.floor(value)
        rows.append(
            {
                "date": day,
                "invested": invested,
                "value": value_yen,
                "after_tax_value": value_yen - tax_on_income(value_yen - invested),
            }
        )
    return pd.DataFrame(rows)
