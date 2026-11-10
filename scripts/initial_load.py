"""初回フル取得：契約プランで取れる全期間を DuckDB に保存する。

途中で止めても、もう一度実行すれば続きから再開する。

    uv run python scripts/initial_load.py                 # 全期間
    uv run python scripts/initial_load.py --limit-days 3  # 動作確認（先頭 3 営業日だけ）
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import date

from core.data.jquants import JQuantsClient, Plan
from core.data.loader import Loader, plan_window
from core.data.store import DEFAULT_DB_PATH, Store


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--start", type=date.fromisoformat, help="開始日（省略時はプランの最古日）")
    p.add_argument("--end", type=date.fromisoformat, help="終了日（省略時はプランの最新日）")
    p.add_argument("--limit-days", type=int, help="先頭からこの営業日数だけ取る（動作確認用）")
    p.add_argument("--skip-fin", action="store_true", help="財務サマリーを取らない")
    p.add_argument("--db", default=str(DEFAULT_DB_PATH), help="DuckDB ファイル")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = JQuantsClient.from_env()
    plan = Plan(os.environ.get("JQUANTS_PLAN") or Plan.FREE)
    start, end = plan_window(plan, date.today())
    start, end = args.start or start, args.end or end
    logging.info("plan=%s 期間 %s 〜 %s → %s", plan, start, end, args.db)

    with Store(args.db) as store:
        result = Loader(client, store).sync(
            start, end, include_fin=not args.skip_fin, limit_days=args.limit_days
        )
    logging.info(
        "完了 取得日数=%s 行数=%s リクエスト=%d", result.fetched, result.rows, result.requests
    )
    for e in result.errors:
        logging.warning("スキップ: %s", e)


if __name__ == "__main__":
    main()
