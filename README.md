# Kabu Lab

日本株の戦略研究プラットフォーム（個人用）。タイムマシン計算・バックテスト・手法ライブラリ・開示書類の読み方ナビ・ペーパートレード。

- 設計方針：`CLAUDE.md`
- 要件定義：`docs/requirements.md`

## セットアップ

```sh
uv sync                    # Python 3.12 と依存パッケージを .venv に導入
cp .env.example .env       # API キーを記入
```

## よく使うコマンド

```sh
uv run pytest              # テスト
uv run ruff check .        # Lint
uv run ruff format .       # フォーマット
uv run streamlit run app/Home.py   # UI 起動
```

※ 本ツールの出力は学習・検証目的であり、投資助言ではありません。
