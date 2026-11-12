"""AI投資委員会 — 投資メモに対して反論・質問を行い、承認／却下を判定する。

使い方:
    from core.paper.committee import InvestmentCommittee, CommitteeRequest

    req = CommitteeRequest(memo=memo, strategy_name="バフェット流")
    for chunk in committee.stream_review(req):
        print(chunk, end="", flush=True)
"""

from __future__ import annotations

import os
from collections.abc import Generator
from dataclasses import dataclass

import anthropic
from dotenv import load_dotenv

from core.paper.investment_memo import InvestmentMemo

load_dotenv()

MODEL = "claude-opus-5-5"

_SYSTEM_PROMPT = """\
あなたは厳格な日本株投資委員会の委員長です。
投資メモを審査し、論理的な穴・リスクの見落とし・根拠の弱さを指摘します。

審査の観点：
1. 投資テーゼの論理的一貫性（なぜ今この銘柄か）
2. カタリストの具体性・実現可能性（いつ・何が起きるか）
3. リスクの網羅性（下振れシナリオは十分か）
4. 撤退条件の明確さ（何があったら売るか）
5. バリュエーション（割高ではないか）

審査結果は以下の形式で出力してください：

## 委員会所見

### 評価できる点
（良い点を2〜3点）

### 質問・懸念事項
1. （最も重要な質問・指摘）
2. （2番目の指摘）
3. （3番目の指摘）

### 暫定判定
承認 / 条件付き承認 / 要追加説明 / 却下

（判定理由を2〜3文で）

注意：本審査は学習・訓練目的であり、実際の投資推奨ではありません。
"""


@dataclass
class CommitteeRequest:
    memo: InvestmentMemo
    strategy_name: str = ""
    additional_context: str = ""


def _build_prompt(req: CommitteeRequest) -> str:
    memo = req.memo
    lines = [
        f"【銘柄】{memo.code} {memo.company_name or ''}",
        f"【手法】{req.strategy_name or memo.strategy or '未指定'}",
        "",
        "## 投資テーゼ",
        memo.thesis,
        "",
        "## カタリスト",
        memo.catalyst,
        "",
        "## リスク",
        memo.risks,
        "",
        "## 撤退条件",
        memo.exit_cond,
    ]
    if memo.target_price:
        lines += ["", f"## 目標株価", f"¥{memo.target_price:,.0f}"]
    if req.additional_context:
        lines += ["", "## 追加情報", req.additional_context]
    lines += ["", "上記の投資メモを審査してください。"]
    return "\n".join(lines)


def stream_review(req: CommitteeRequest) -> Generator[str, None, None]:
    """AI委員会の審査をストリーミングで返す。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が設定されていません。")

    client = anthropic.Anthropic(api_key=api_key)

    with client.messages.stream(
        model=MODEL,
        max_tokens=2048,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_prompt(req)}],
    ) as stream:
        for text in stream.text_stream:
            yield text


def get_review(req: CommitteeRequest) -> str:
    """審査結果の全文を返す（非ストリーミング）。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が設定されていません。")

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_prompt(req)}],
    )
    return next((b.text for b in response.content if b.type == "text"), "")


def stream_monthly_report(
    account_summary: dict,
    trades: list[dict],
    nav_history: list[dict],
) -> Generator[str, None, None]:
    """月次運用報告書をストリーミング生成する。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が設定されていません。")

    system = """\
あなたは運用会社のファンドマネージャーです。
月次運用報告書を、投資家（自分自身）向けに日本語で作成してください。

報告書の構成：
1. 月間パフォーマンスサマリー
2. 主要取引の振り返りと投資理由の妥当性評価
3. 保有銘柄の現状評価
4. 来月の方針・注目点
5. 自己評価・改善点

客観的かつ率直に、良い点も悪い点も記載してください。
本報告書は学習・訓練目的であり、投資推奨ではありません。
"""

    import json as _json
    prompt_parts = [
        "## 口座サマリー",
        _json.dumps(account_summary, ensure_ascii=False, indent=2),
        "",
        "## 今月の取引",
        _json.dumps(trades[:20], ensure_ascii=False, indent=2),
        "",
        "## NAV推移",
        _json.dumps(nav_history[-10:], ensure_ascii=False, indent=2),
        "",
        "上記データをもとに月次運用報告書を作成してください。",
    ]

    client = anthropic.Anthropic(api_key=api_key)
    with client.messages.stream(
        model=MODEL,
        max_tokens=3000,
        system=system,
        messages=[{"role": "user", "content": "\n".join(prompt_parts)}],
    ) as stream:
        for text in stream.text_stream:
            yield text
