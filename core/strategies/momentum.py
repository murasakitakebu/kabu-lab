"""モメンタム投資。

過去12ヶ月リターン（直近1ヶ月除く）の上位銘柄を保有する。
Jegadeesh & Titman (1993) のモメンタム効果を日本株に適用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from core.strategies.base import ReportFocus, ScreenResult, Strategy, StrategyMeta


@dataclass
class MomentumStrategy(Strategy):
    """モメンタム投資。

    Args:
        lookback_days: モメンタム計算のルックバック期間（日数）。約252営業日＝1年
        skip_days: 直近スキップ期間（日数）。約21営業日＝1ヶ月
        top_n: 上位何銘柄を返すか
        min_market_cap: 時価総額の下限（円）
    """

    lookback_days: int = 252
    skip_days: int = 21
    top_n: int = 30
    min_market_cap: int = 10_000_000_000  # 100億円

    meta = StrategyMeta(
        id="momentum",
        name="モメンタム",
        proposer="Jegadeesh & Titman（1993）／クリフ・アスネス（AQR）",
        summary=(
            "「上がってきた株はしばらく上がり続ける」という価格モメンタム効果を利用する。"
            "過去12ヶ月のリターン（直近1ヶ月を除く）が高い銘柄を等ウェイトで保有し、"
            "1〜3ヶ月ごとにリバランスする。"
            "バリューとの相関が低くポートフォリオの分散効果が高い。"
            "学術的にも実務的にも最も頑健なファクターの一つとされている。"
        ),
        rebalance_hint="月次または四半期（リバランスコストとの兼ね合いで決める）",
        weaknesses=(
            "相場の急転換（クラッシュ）時に大きく負けるモメンタムクラッシュがある。"
            "高回転により取引コスト・税金が嵩みやすい（特に短期保有の場合）。"
            "急落後の銘柄を掴みにくく、反転局面で乗り遅れやすい。"
        ),
        references=[
            "Jegadeesh & Titman (1993) Returns to Buying Winners and Selling Losers",
            "ファクター投資入門（アンドリュー・L・バーキン、ラリー・E・スウェドロー）",
        ],
        report_focuses=[
            ReportFocus("決算短信", "業績予想の修正", "上方修正が続く企業は業績モメンタムもあわせて確認する"),
            ReportFocus("決算短信", "1ページ目（売上・利益推移）", "業績の加速・減速のサインを株価モメンタムと照合する"),
        ],
    )

    def screen(
        self,
        fin: pd.DataFrame,
        prices: pd.DataFrame,
        as_of: date,
    ) -> list[ScreenResult]:
        # prices には price_history が必要だが、このメソッドのシグネチャでは
        # 直近株価しか受け取らない。モメンタムは全銘柄の過去株価を必要とするため、
        # screen_with_history() を使う設計にしている。
        # 標準 screen() では価格履歴なしで動かせないため空リストを返す。
        return []

    def screen_with_history(
        self,
        prices_history: pd.DataFrame,
        listed_info: pd.DataFrame,
        as_of: date,
    ) -> list[ScreenResult]:
        """価格履歴を使ったモメンタムスクリーニング。

        Args:
            prices_history: 全銘柄の日足。列 `code`, `date`, `close`。
            listed_info: 銘柄一覧。列 `code`, `name`。
            as_of: スクリーニング基準日。
        """
        start = as_of - timedelta(days=self.lookback_days + 30)
        hist = prices_history[
            (prices_history["date"] >= start) & (prices_history["date"] <= as_of)
        ].copy()

        rows = []
        for code, grp in hist.groupby("code"):
            grp = grp.sort_values("date")
            if len(grp) < self.lookback_days // 2:
                continue

            # 時価総額フィルタ（直近終値で近似）
            last_close = grp["close"].iloc[-1]

            # 直近スキップ後の価格 (t-21 相当)
            if len(grp) < self.skip_days + 1:
                continue
            end_close = grp["close"].iloc[-(self.skip_days + 1)]

            # ルックバック開始時点の価格
            start_close = grp["close"].iloc[0]
            if start_close <= 0 or end_close <= 0:
                continue

            momentum = end_close / start_close - 1
            rows.append({"code": str(code), "momentum": momentum, "close": last_close})

        if not rows:
            return []

        df = pd.DataFrame(rows)
        df = df.sort_values("momentum", ascending=False).head(self.top_n)
        name_map = dict(zip(listed_info["code"], listed_info["name"]))

        return [
            ScreenResult(
                code=r.code,
                name=name_map.get(r.code, r.code),
                score=r.momentum,
                metrics={"モメンタム(12-1ヶ月)": round(r.momentum, 4)},
                as_of=as_of,
            )
            for r in df.itertuples(index=False)
        ]
