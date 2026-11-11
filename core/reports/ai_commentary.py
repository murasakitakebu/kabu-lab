"""Claude API による有報・決算短信の AI 解説機能。

手法と書類テキストを受け取り、Claude が「この数字はこう読む」「前年からの変化」
「赤信号の有無」を解説するストリーミング応答を返す。

使い方:
    from core.reports.ai_commentary import CommentaryRequest, stream_commentary

    for chunk in stream_commentary(CommentaryRequest(...)):
        print(chunk, end="", flush=True)
"""

from __future__ import annotations

import os
from collections.abc import Generator
from dataclasses import dataclass, field

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-opus-5-5"

_SYSTEM_PROMPT = """\
あなたは日本株の有価証券報告書・決算短信の読み方を教える投資教育アシスタントです。

ユーザーが指定した投資手法の視点から、提示された書類テキストを分析し、
以下の3点を日本語でわかりやすく解説してください。

1. **この数字の読み方**：手法の観点から重要な指標・数値と、その意味
2. **前年比での変化**：改善・悪化のポイントと、その背景
3. **注目点・警戒点**：手法のチェックリストに照らした所見

注意事項:
- 本解説は学習・研究目的です。投資推奨ではありません。
- 推測は「〜と見られます」「〜の可能性があります」と表現してください。
- テキストに記載のない事実は述べないでください。
- 専門用語は初めて出てくる場合に簡単な説明を加えてください。
"""


@dataclass
class CommentaryRequest:
    """AI 解説リクエストのパラメータ。"""

    strategy_name: str           # 手法名（例: バフェット流）
    company_name: str            # 会社名
    doc_description: str         # 書類種別（例: 有価証券報告書）
    submit_date: str             # 提出日
    text_excerpt: str            # 解説対象のテキスト抜粋（最大 ~8000 字推奨）
    focus_sections: list[str] = field(default_factory=list)  # 重点的に読むセクション


def _build_user_prompt(req: CommentaryRequest) -> str:
    sections_hint = ""
    if req.focus_sections:
        sections_hint = "\n".join(f"- {s}" for s in req.focus_sections)
        sections_hint = f"\n\n特に以下の箇所に着目して解説してください:\n{sections_hint}"

    return f"""\
【手法】{req.strategy_name}
【会社名】{req.company_name}
【書類】{req.doc_description}（{req.submit_date}）{sections_hint}

--- 書類テキスト（抜粋）---
{req.text_excerpt[:8000]}
---

上記の書類テキストを、{req.strategy_name}の観点から解説してください。"""


def stream_commentary(req: CommentaryRequest) -> Generator[str, None, None]:
    """Claude API を使って書類の AI 解説をストリーミングで返す。

    Yields
    ------
    str
        テキストの差分チャンク
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError(
            "ANTHROPIC_API_KEY が設定されていません。.env ファイルを確認してください。"
        )

    client = anthropic.Anthropic(api_key=api_key)

    with client.messages.stream(
        model=MODEL,
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_prompt(req)}],
    ) as stream:
        for text in stream.text_stream:
            yield text


def get_commentary(req: CommentaryRequest) -> str:
    """ストリーミングなしで解説全文を返す（テスト用）。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が設定されていません。")

    client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_prompt(req)}],
    )
    return next(
        (block.text for block in response.content if block.type == "text"), ""
    )
