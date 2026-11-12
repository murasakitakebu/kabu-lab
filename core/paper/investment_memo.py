"""投資メモ（テーゼ・カタリスト・リスク・撤退条件）の管理。

DuckDB に保存し、AI投資委員会の審議履歴も記録する。
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "kabulab.duckdb"

MEMO_SCHEMA_SQL = """
-- 投資メモ
CREATE TABLE IF NOT EXISTS investment_memos (
    memo_id      VARCHAR  PRIMARY KEY,
    account_id   VARCHAR  NOT NULL DEFAULT 'default',
    code         VARCHAR  NOT NULL,
    company_name VARCHAR,
    thesis       VARCHAR  NOT NULL,  -- 投資テーゼ
    catalyst     VARCHAR  NOT NULL,  -- カタリスト
    risks        VARCHAR  NOT NULL,  -- リスク
    exit_cond    VARCHAR  NOT NULL,  -- 撤退条件
    target_price DOUBLE,
    strategy     VARCHAR,
    status       VARCHAR  NOT NULL DEFAULT 'draft',  -- draft / approved / rejected / closed
    created_at   TIMESTAMP NOT NULL,
    updated_at   TIMESTAMP NOT NULL
);

-- AI投資委員会の審議履歴
CREATE TABLE IF NOT EXISTS committee_reviews (
    review_id    VARCHAR  PRIMARY KEY,
    memo_id      VARCHAR  NOT NULL,
    questions    VARCHAR  NOT NULL,  -- JSON: AI からの質問リスト
    answers      VARCHAR,            -- JSON: ユーザーの回答
    verdict      VARCHAR,            -- approved / rejected / pending
    ai_summary   VARCHAR,            -- AI の最終所見
    created_at   TIMESTAMP NOT NULL,
    answered_at  TIMESTAMP
);
"""


@dataclass
class InvestmentMemo:
    memo_id: str
    account_id: str
    code: str
    company_name: str | None
    thesis: str
    catalyst: str
    risks: str
    exit_cond: str
    target_price: float | None
    strategy: str | None
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass
class CommitteeReview:
    review_id: str
    memo_id: str
    questions: list[str]
    answers: list[str] | None
    verdict: str | None
    ai_summary: str | None
    created_at: datetime
    answered_at: datetime | None


class MemoStore:
    """投資メモと委員会審議の永続化。"""

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
            conn.execute(MEMO_SCHEMA_SQL)

    def save_memo(self, memo: InvestmentMemo) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO investment_memos VALUES
                   (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    memo.memo_id, memo.account_id, memo.code, memo.company_name,
                    memo.thesis, memo.catalyst, memo.risks, memo.exit_cond,
                    memo.target_price, memo.strategy, memo.status,
                    memo.created_at, memo.updated_at,
                ],
            )

    def get_memo(self, memo_id: str) -> InvestmentMemo | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM investment_memos WHERE memo_id = ?", [memo_id]
            ).fetchone()
        if not row:
            return None
        return InvestmentMemo(*row)

    def list_memos(self, account_id: str = "default") -> pd.DataFrame:
        with self._conn() as conn:
            return conn.execute(
                """SELECT memo_id, code, company_name, thesis, status, strategy,
                          target_price, created_at
                   FROM investment_memos WHERE account_id = ?
                   ORDER BY created_at DESC""",
                [account_id],
            ).df()

    def update_status(self, memo_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE investment_memos SET status = ?, updated_at = ? WHERE memo_id = ?",
                [status, datetime.now(JST), memo_id],
            )

    def save_review(self, review: CommitteeReview) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO committee_reviews VALUES
                   (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    review.review_id, review.memo_id,
                    json.dumps(review.questions, ensure_ascii=False),
                    json.dumps(review.answers, ensure_ascii=False) if review.answers else None,
                    review.verdict, review.ai_summary,
                    review.created_at, review.answered_at,
                ],
            )

    def get_review(self, memo_id: str) -> CommitteeReview | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM committee_reviews WHERE memo_id = ? ORDER BY created_at DESC LIMIT 1",
                [memo_id],
            ).fetchone()
        if not row:
            return None
        return CommitteeReview(
            review_id=row[0],
            memo_id=row[1],
            questions=json.loads(row[2]),
            answers=json.loads(row[3]) if row[3] else None,
            verdict=row[4],
            ai_summary=row[5],
            created_at=row[6],
            answered_at=row[7],
        )
