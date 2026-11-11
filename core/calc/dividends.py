"""決算短信サマリーからの配当推定（J-Quants Free プラン用の代替）。

配当金情報 API（Premium のみ）が使えないため、決算短信の「配当の状況」の実績値
（Div1Q / Div2Q / Div3Q / DivFY）から 1 株配当と基準日を推定する。

- 基準日：各四半期末（期首 + 3, 6, 9 か月 - 1 日）と期末日。多くの会社はこの日を基準日にしている
- 権利落ち日：受渡しが T+2（2019 年 7 月〜）なので、基準日以前の最終営業日の 1 営業日前
- 支払日：基準日の約 75 日後（3 月期末配当は 6 月下旬、中間配当は 12 月上旬が多い）の近似
- 同じ基準日の実績が複数の短信にあるときは、基準日後で最も早い開示を採る
  （後の短信は株式分割を反映して修正されている場合があるため）
"""

from __future__ import annotations

import bisect
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import pandas as pd

from core.calc.returns import Dividend

#: 決算短信の配当実績フィールドと、期首からの月数
_DIV_FIELDS = [("Div1Q", 3), ("Div2Q", 6), ("Div3Q", 9), ("DivFY", 12)]

PAY_LAG_DAYS = 75


def ex_date_for_record(record: date, trading_days: list[date]) -> date:
    """基準日から権利落ち日を求める（T+2 受渡し）。

    基準日以前で最後の営業日 S に受渡しが間に合う最後の約定日（権利付最終日）は S の 2 営業日前、
    権利落ち日はその翌営業日 = S の 1 営業日前。
    """
    i = bisect.bisect_right(trading_days, record) - 1  # S の位置
    if i < 1:
        raise ValueError(f"{record} の前の営業日がカレンダーにありません")
    return trading_days[i - 1]


def _add_months_minus_day(start: date, months: int) -> date:
    return (pd.Timestamp(start) + pd.DateOffset(months=months)).date() - timedelta(days=1)


def _dec(v: object) -> Decimal | None:
    if v is None:
        return None
    s = str(v).strip()
    if s in {"", "-", "－"}:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _next_trading_day(d: date, trading_days: list[date]) -> date:
    i = bisect.bisect_left(trading_days, d)
    return trading_days[i] if i < len(trading_days) else d


def estimate_dividends(fin: pd.DataFrame, trading_days: list[date]) -> list[Dividend]:
    """1 銘柄の決算短信サマリー（`raw` 列つき）から配当履歴を推定する。

    Args:
        fin: store の fin_summary の行（列 `raw`, `disclosed_date` が必要）
        trading_days: 取引カレンダーの営業日（昇順）
    """
    found: dict[date, tuple[date, Decimal]] = {}  # 基準日 -> (開示日, 1株配当)
    for raw, disclosed in zip(fin["raw"], fin["disclosed_date"], strict=True):
        rec = json.loads(raw) if isinstance(raw, str) else raw
        disclosed = pd.Timestamp(disclosed).date()
        fy_start = pd.to_datetime(rec.get("CurFYSt"), errors="coerce")
        if pd.isna(fy_start):
            continue
        for fld, months in _DIV_FIELDS:
            amount = _dec(rec.get(fld))
            if amount is None or amount <= 0:
                continue
            record = _add_months_minus_day(fy_start.date(), months)
            if disclosed < record:  # 基準日前の開示に「実績」は無いはず
                continue
            prev = found.get(record)
            if prev is None or disclosed < prev[0]:
                found[record] = (disclosed, amount)

    out = []
    for record, (_, amount) in sorted(found.items()):
        try:
            ex = ex_date_for_record(record, trading_days)
        except ValueError:
            continue
        pay = _next_trading_day(record + timedelta(days=PAY_LAG_DAYS), trading_days)
        out.append(Dividend(ex, pay, amount, estimated=True))
    return out
