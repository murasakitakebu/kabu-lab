"""ペーパートレード — ポートフォリオ管理エンジン。

DuckDB にポートフォリオ・取引履歴・NAV推移を保存する。
約定は「翌営業日始値」が理想だが、J-Quants Free プランでは当日終値を使う。
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "kabulab.duckdb"

PAPER_SCHEMA_SQL = """
-- ペーパートレード口座
CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id    VARCHAR  PRIMARY KEY,
    name          VARCHAR  NOT NULL,
    initial_cash  BIGINT   NOT NULL,
    cash          BIGINT   NOT NULL,
    created_at    TIMESTAMP NOT NULL,
    note          VARCHAR
);

-- 保有銘柄
CREATE TABLE IF NOT EXISTS paper_positions (
    account_id  VARCHAR  NOT NULL,
    code        VARCHAR  NOT NULL,
    shares      BIGINT   NOT NULL,
    avg_cost    DOUBLE   NOT NULL,  -- 取得単価（円）
    PRIMARY KEY (account_id, code)
);

-- 取引履歴
CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id      VARCHAR  PRIMARY KEY,
    account_id    VARCHAR  NOT NULL,
    code          VARCHAR  NOT NULL,
    side          VARCHAR  NOT NULL,  -- 'buy' | 'sell'
    shares        BIGINT   NOT NULL,
    price         DOUBLE   NOT NULL,
    amount        BIGINT   NOT NULL,  -- 約定金額（円）= shares × price
    fee           BIGINT   NOT NULL DEFAULT 0,
    trade_date    DATE     NOT NULL,
    reason        VARCHAR  NOT NULL,  -- 投資理由（必須）
    strategy      VARCHAR,
    created_at    TIMESTAMP NOT NULL
);

-- NAV（純資産）推移
CREATE TABLE IF NOT EXISTS paper_nav_history (
    account_id  VARCHAR  NOT NULL,
    nav_date    DATE     NOT NULL,
    cash        BIGINT   NOT NULL,
    stocks_value BIGINT  NOT NULL,
    nav         BIGINT   NOT NULL,
    PRIMARY KEY (account_id, nav_date)
);
"""

DEFAULT_ACCOUNT_ID = "default"
DEFAULT_INITIAL_CASH = 10_000_000  # 1,000万円


@dataclass
class Position:
    code: str
    shares: int
    avg_cost: float
    current_price: float = 0.0

    @property
    def market_value(self) -> int:
        return int(self.shares * self.current_price)

    @property
    def unrealized_pnl(self) -> int:
        return int(self.shares * (self.current_price - self.avg_cost))

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.avg_cost == 0:
            return 0.0
        return (self.current_price - self.avg_cost) / self.avg_cost * 100


@dataclass
class TradeRecord:
    trade_id: str
    account_id: str
    code: str
    side: str  # 'buy' | 'sell'
    shares: int
    price: float
    amount: int
    fee: int
    trade_date: date
    reason: str
    strategy: str | None
    created_at: datetime


@dataclass
class Account:
    account_id: str
    name: str
    initial_cash: int
    cash: int
    created_at: datetime
    note: str | None = None


class PaperPortfolio:
    """ペーパートレードのポートフォリオ操作クラス。"""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _conn(self):
        conn = duckdb.connect(str(self._db_path))
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(PAPER_SCHEMA_SQL)

    # ------------------------------------------------------------------
    # 口座操作
    # ------------------------------------------------------------------

    def get_or_create_account(
        self,
        account_id: str = DEFAULT_ACCOUNT_ID,
        name: str = "デフォルト口座",
        initial_cash: int = DEFAULT_INITIAL_CASH,
    ) -> Account:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM paper_accounts WHERE account_id = ?", [account_id]
            ).fetchone()
            if row:
                return Account(*row)
            now = datetime.now(JST)
            conn.execute(
                """INSERT INTO paper_accounts VALUES (?, ?, ?, ?, ?, ?)""",
                [account_id, name, initial_cash, initial_cash, now, None],
            )
            return Account(account_id, name, initial_cash, initial_cash, now)

    def get_account(self, account_id: str = DEFAULT_ACCOUNT_ID) -> Account | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM paper_accounts WHERE account_id = ?", [account_id]
            ).fetchone()
            if not row:
                return None
            return Account(*row)

    def reset_account(
        self,
        account_id: str = DEFAULT_ACCOUNT_ID,
        initial_cash: int = DEFAULT_INITIAL_CASH,
    ) -> Account:
        """口座をリセットする（取引履歴・ポジションも削除）。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM paper_trades WHERE account_id = ?", [account_id])
            conn.execute("DELETE FROM paper_positions WHERE account_id = ?", [account_id])
            conn.execute("DELETE FROM paper_nav_history WHERE account_id = ?", [account_id])
            conn.execute("DELETE FROM paper_accounts WHERE account_id = ?", [account_id])
        return self.get_or_create_account(account_id, initial_cash=initial_cash)

    # ------------------------------------------------------------------
    # 売買
    # ------------------------------------------------------------------

    def buy(
        self,
        code: str,
        shares: int,
        price: float,
        reason: str,
        trade_date: date | None = None,
        strategy: str | None = None,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> TradeRecord:
        """買い注文を執行する。"""
        if not reason.strip():
            raise ValueError("投資理由（reason）は必須です。")
        if shares <= 0:
            raise ValueError("株数は1以上を指定してください。")
        if price <= 0:
            raise ValueError("価格は正の値を指定してください。")

        amount = int(shares * price)
        trade_date = trade_date or date.today()

        with self._conn() as conn:
            acct = conn.execute(
                "SELECT cash FROM paper_accounts WHERE account_id = ?", [account_id]
            ).fetchone()
            if not acct:
                raise ValueError(f"口座 {account_id} が存在しません。先に作成してください。")
            if acct[0] < amount:
                raise ValueError(
                    f"資金不足です。必要: {amount:,}円、残高: {acct[0]:,}円"
                )

            trade_id = f"{account_id}_{code}_{trade_date}_{datetime.now(JST).strftime('%H%M%S%f')}"
            now = datetime.now(JST)

            conn.execute(
                """INSERT INTO paper_trades VALUES (?, ?, ?, 'buy', ?, ?, ?, 0, ?, ?, ?, ?)""",
                [trade_id, account_id, code, shares, price, amount, trade_date, reason, strategy, now],
            )
            conn.execute(
                "UPDATE paper_accounts SET cash = cash - ? WHERE account_id = ?",
                [amount, account_id],
            )

            # ポジション更新（平均コスト法）
            pos = conn.execute(
                "SELECT shares, avg_cost FROM paper_positions WHERE account_id = ? AND code = ?",
                [account_id, code],
            ).fetchone()
            if pos:
                old_shares, old_cost = pos
                new_shares = old_shares + shares
                new_cost = (old_shares * old_cost + shares * price) / new_shares
                conn.execute(
                    "UPDATE paper_positions SET shares = ?, avg_cost = ? WHERE account_id = ? AND code = ?",
                    [new_shares, new_cost, account_id, code],
                )
            else:
                conn.execute(
                    "INSERT INTO paper_positions VALUES (?, ?, ?, ?)",
                    [account_id, code, shares, price],
                )

        logger.info("BUY %s %d株 @%.0f円 (%s)", code, shares, price, reason[:30])
        return TradeRecord(trade_id, account_id, code, "buy", shares, price, amount, 0, trade_date, reason, strategy, now)

    def sell(
        self,
        code: str,
        shares: int,
        price: float,
        reason: str,
        trade_date: date | None = None,
        strategy: str | None = None,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> TradeRecord:
        """売り注文を執行する。"""
        if not reason.strip():
            raise ValueError("投資理由（reason）は必須です。")
        if shares <= 0:
            raise ValueError("株数は1以上を指定してください。")

        amount = int(shares * price)
        trade_date = trade_date or date.today()

        with self._conn() as conn:
            pos = conn.execute(
                "SELECT shares FROM paper_positions WHERE account_id = ? AND code = ?",
                [account_id, code],
            ).fetchone()
            if not pos or pos[0] < shares:
                held = pos[0] if pos else 0
                raise ValueError(
                    f"保有株数が不足しています。保有: {held}株、売却指定: {shares}株"
                )

            trade_id = f"{account_id}_{code}_{trade_date}_{datetime.now(JST).strftime('%H%M%S%f')}"
            now = datetime.now(JST)

            conn.execute(
                """INSERT INTO paper_trades VALUES (?, ?, ?, 'sell', ?, ?, ?, 0, ?, ?, ?, ?)""",
                [trade_id, account_id, code, shares, price, amount, trade_date, reason, strategy, now],
            )
            conn.execute(
                "UPDATE paper_accounts SET cash = cash + ? WHERE account_id = ?",
                [amount, account_id],
            )

            new_shares = pos[0] - shares
            if new_shares == 0:
                conn.execute(
                    "DELETE FROM paper_positions WHERE account_id = ? AND code = ?",
                    [account_id, code],
                )
            else:
                conn.execute(
                    "UPDATE paper_positions SET shares = ? WHERE account_id = ? AND code = ?",
                    [new_shares, account_id, code],
                )

        logger.info("SELL %s %d株 @%.0f円 (%s)", code, shares, price, reason[:30])
        return TradeRecord(trade_id, account_id, code, "sell", shares, price, amount, 0, trade_date, reason, strategy, now)

    # ------------------------------------------------------------------
    # 照会
    # ------------------------------------------------------------------

    def get_positions(self, account_id: str = DEFAULT_ACCOUNT_ID) -> list[Position]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT code, shares, avg_cost FROM paper_positions WHERE account_id = ? ORDER BY code",
                [account_id],
            ).fetchall()
        return [Position(code=r[0], shares=r[1], avg_cost=r[2]) for r in rows]

    def get_trades(
        self,
        account_id: str = DEFAULT_ACCOUNT_ID,
        limit: int = 100,
    ) -> pd.DataFrame:
        with self._conn() as conn:
            df = conn.execute(
                """SELECT trade_id, code, side, shares, price, amount, trade_date, reason, strategy, created_at
                   FROM paper_trades WHERE account_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                [account_id, limit],
            ).df()
        return df

    def save_nav(
        self,
        nav_date: date,
        positions: list[Position],
        cash: int,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> None:
        """NAV を記録する（日次呼び出し想定）。"""
        stocks_value = sum(p.market_value for p in positions)
        nav = cash + stocks_value
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO paper_nav_history VALUES (?, ?, ?, ?, ?)""",
                [account_id, nav_date, cash, stocks_value, nav],
            )

    def get_nav_history(self, account_id: str = DEFAULT_ACCOUNT_ID) -> pd.DataFrame:
        with self._conn() as conn:
            df = conn.execute(
                """SELECT nav_date, cash, stocks_value, nav
                   FROM paper_nav_history WHERE account_id = ?
                   ORDER BY nav_date""",
                [account_id],
            ).df()
        return df
