"""差分更新：新しく取得できるようになった日の分を取り足す。

Free プランは直近 12 週間が取れないため、新しく増えるのは 1 日 1 営業日分。週 1 回の実行で十分。
株式分割・併合があった銘柄は、過去の調整後価格が書き換わるので全期間を取り直す。

    uv run python scripts/daily_update.py
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import date, timedelta

from core.data.jquants import JQuantsClient, Plan
from core.data.loader import Loader, plan_window
from core.data.store import DEFAULT_DB_PATH, Store


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--lookback", type=int, default=45, help="さかのぼって確認する日数（既定 45）")
    p.add_argument("--db", default=str(DEFAULT_DB_PATH), help="DuckDB ファイル")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = JQuantsClient.from_env()
    plan = Plan(os.environ.get("JQUANTS_PLAN") or Plan.FREE)
    history_start, end = plan_window(plan, date.today())
    start = max(history_start, end - timedelta(days=args.lookback))
    logging.info("plan=%s 期間 %s 〜 %s → %s", plan, start, end, args.db)

    with Store(args.db) as store:
        result = Loader(client, store).sync(
            start, end, refresh_splits=True, history_start=history_start
        )
    logging.info(
        "完了 取得日数=%s 行数=%s リクエスト=%d", result.fetched, result.rows, result.requests
    )
    for e in result.errors:
        logging.warning("スキップ: %s", e)


if __name__ == "__main__":
    main()
