"""手法ライブラリの基底クラスとスクリーニング条件の型定義。

各手法は `Strategy` を継承し、`screen()` でスクリーニング結果を返す。
`screen()` には「`as_of` 時点で開示済みの財務データだけ使う」というルックアヘッド
防止の責務がある。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class ScreenResult:
    """スクリーニング結果の1行。

    Attributes:
        code: 銘柄コード（5桁）
        name: 社名
        score: 手法ごとのスコア（高い方が条件に合致。None なら順位付けなし）
        metrics: 判定に使った指標（キー=指標名、値=数値）
        as_of: スクリーニング基準日（開示日ベース）
    """

    code: str
    name: str
    score: float | None
    metrics: dict[str, Any]
    as_of: date


@dataclass(frozen=True)
class ReportFocus:
    """読み方ナビで注目する開示書類の箇所。"""

    document: str   # 例: "有価証券報告書", "決算短信"
    section: str    # 例: "事業の内容", "キャッシュフロー計算書"
    reason: str     # なぜその手法でこの箇所が重要か


@dataclass(frozen=True)
class StrategyMeta:
    """手法の静的メタ情報（パラメータによらず固定）。"""

    id: str                          # スネークケースの識別子
    name: str                        # 表示名
    proposer: str                    # 提唱者
    summary: str                     # 考え方の要約（300字程度）
    rebalance_hint: str              # リバランス頻度の推奨
    weaknesses: str                  # 弱点・向かない相場
    references: list[str]            # 参考文献
    report_focuses: list[ReportFocus]  # 読み方ナビの重点箇所


class Strategy(ABC):
    """手法の基底クラス。

    具体的な手法は `meta` クラス変数と `screen()` メソッドを実装する。
    パラメータは `__init__` で受け取る（デフォルト値は要件書の例示値）。
    """

    meta: StrategyMeta  # サブクラスで上書きする

    @abstractmethod
    def screen(
        self,
        fin: pd.DataFrame,
        prices: pd.DataFrame,
        as_of: date,
    ) -> list[ScreenResult]:
        """スクリーニングを実行する。

        Args:
            fin: `store.fin_summary_known_at(code, as_of)` 相当の全銘柄決算データ。
                 列は `fin_summary` テーブルのカラム。複数期分含む場合は最新期を使う。
            prices: 銘柄ごとの直近株価。列 `code`, `close`, `name`。
            as_of: スクリーニング基準日（この日以前に開示されたデータだけ使う）。

        Returns:
            条件を満たす銘柄の `ScreenResult` リスト（スコア降順推奨）。
        """

    @classmethod
    def describe(cls) -> str:
        """手法の概要を人間が読める形で返す（CLI・UI 向け）。"""
        m = cls.meta
        lines = [
            f"【{m.name}】（{m.proposer}）",
            "",
            m.summary,
            "",
            f"リバランス：{m.rebalance_hint}",
            f"弱点：{m.weaknesses}",
        ]
        if m.references:
            lines += ["", "参考文献："] + [f"  - {r}" for r in m.references]
        if m.report_focuses:
            lines += ["", "読み方ナビ重点箇所："]
            for f_ in m.report_focuses:
                lines.append(f"  [{f_.document}] {f_.section} — {f_.reason}")
        return "\n".join(lines)
