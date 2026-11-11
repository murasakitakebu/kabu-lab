"""Phase 3: 読み方ナビ — EDINET書類の手法別ガイド。

機能:
- 銘柄・期間を指定して EDINET から有報・決算短信の一覧を取得
- 選択した手法の「読むべき箇所マップ」を表示
- 赤信号チェックリスト
- PDF を直接ブラウザで表示
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="読み方ナビ", page_icon="📖", layout="wide")

st.title("📖 読み方ナビ")
st.caption("有価証券報告書・決算短信の「どこを読むべきか」を手法別に案内します。")
st.warning("⚠️ 本ツールの出力は学習・研究目的です。投資助言ではありません。", icon="⚠️")

# ---------------------------------------------------------------------------
# imports（エラーを画面に表示）
# ---------------------------------------------------------------------------
try:
    from core.data.edinet import (
        ANNUAL_REPORT_CODES,
        DOC_TYPE_CODES,
        TANSHIN_CODES,
        EdinetClient,
    )
    from core.reports.reading_map import (
        STRATEGY_GUIDES,
        get_all_reading_points,
        get_common_red_flags,
        get_guide,
    )

    EDINET_AVAILABLE = bool(os.environ.get("EDINET_API_KEY"))
except ImportError as e:
    st.error(f"モジュールのインポートに失敗しました: {e}")
    st.stop()

STRATEGY_LABELS: dict[str, str] = {
    "buffett": "バフェット流（クオリティ）",
    "graham": "グレアム（ネットネット）",
    "greenblatt": "グリーンブラット（魔法の公式）",
    "lynch": "リンチ（PEG）",
    "high_dividend": "高配当・累進配当",
    "tse_reform": "東証改革テーマ",
    "momentum": "モメンタム",
}

# ---------------------------------------------------------------------------
# サイドバー：設定
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ 設定")
    selected_strategy = st.selectbox(
        "投資手法",
        options=list(STRATEGY_LABELS.keys()),
        format_func=lambda k: STRATEGY_LABELS[k],
        index=0,
    )

    st.divider()
    st.subheader("EDINET 書類検索")

    sec_code_input = st.text_input(
        "証券コード（4桁）",
        placeholder="例: 7203",
        help="東証上場企業の証券コード4桁を入力してください。",
    )

    today = datetime.date.today()
    col_s, col_e = st.columns(2)
    with col_s:
        start_date = st.date_input(
            "開始日",
            value=today - datetime.timedelta(days=180),
            max_value=today,
        )
    with col_e:
        end_date = st.date_input(
            "終了日",
            value=today,
            max_value=today,
        )

    doc_type_selection = st.multiselect(
        "書類種別",
        options=list(DOC_TYPE_CODES.keys()),
        default=["010", "120", "160"],
        format_func=lambda k: f"{k}：{DOC_TYPE_CODES[k]}",
    )

    search_btn = st.button("🔍 書類を検索", use_container_width=True, type="primary")

# ---------------------------------------------------------------------------
# メイン：手法別 読み方マップ
# ---------------------------------------------------------------------------
tab_map, tab_docs, tab_redflags = st.tabs(["📋 読み方マップ", "📄 書類一覧", "🚩 赤信号チェック"])

# --- タブ1: 読み方マップ ---
with tab_map:
    guide = get_guide(selected_strategy)
    if guide is None:
        st.error("ガイドが見つかりません。")
    else:
        st.subheader(f"{guide.strategy_name} — 読み方ガイド")
        if guide.summary:
            st.info(guide.summary)

        reading_points = get_all_reading_points(selected_strategy)
        if not reading_points:
            st.write("読み方マップがありません。")
        else:
            priority_labels = {1: "🔴 必須", 2: "🟡 推奨", 3: "🔵 参考"}

            for rp in reading_points:
                priority_badge = priority_labels.get(rp.priority, "")
                with st.expander(f"{priority_badge}  {rp.section}", expanded=(rp.priority == 1)):
                    st.write(rp.what_you_learn)
                    related = [STRATEGY_LABELS.get(s, s) for s in rp.strategies]
                    st.caption(f"関連手法: {' / '.join(related)}")
                    if rp.xbrl_element:
                        st.caption(f"XBRL 要素: `{rp.xbrl_element}`")

# --- タブ2: 書類一覧 ---
with tab_docs:
    if not EDINET_AVAILABLE:
        st.warning(
            "EDINET_API_KEY が設定されていません。`.env` ファイルに `EDINET_API_KEY=<key>` を追加してください。",
            icon="🔑",
        )
        st.markdown(
            """
**EDINET Subscription Key の取得方法:**
1. [EDINET](https://disclosure2.edinet-fsa.go.jp/) にアクセス
2. 「API利用登録」から無料で Subscription Key を取得
3. `.env` ファイルに `EDINET_API_KEY=<取得したキー>` を追加して再起動
"""
        )
        st.stop()

    if not search_btn:
        st.info("左のサイドバーで条件を設定して「書類を検索」をクリックしてください。")
    else:
        if not sec_code_input.strip():
            st.error("証券コードを入力してください。")
            st.stop()

        sec_code = sec_code_input.strip().zfill(4)

        with st.spinner(f"{sec_code} の書類を EDINET から取得中…"):
            try:
                client = EdinetClient()
                docs = client.get_doc_list_by_sec_code(
                    sec_code=sec_code,
                    start=start_date,
                    end=end_date,
                    doc_type_codes=doc_type_selection if doc_type_selection else None,
                )
            except Exception as exc:
                st.error(f"EDINET API エラー: {exc}")
                st.stop()

        if not docs:
            st.warning(
                f"指定期間（{start_date} 〜 {end_date}）に該当する書類が見つかりませんでした。"
                "期間や書類種別を変更してみてください。"
            )
        else:
            st.success(f"{len(docs)} 件の書類が見つかりました。")

            import pandas as pd

            df = pd.DataFrame(
                [
                    {
                        "提出日": d.submit_date,
                        "会社名": d.company_name,
                        "書類種別": DOC_TYPE_CODES.get(d.doc_type_code, d.doc_type_code),
                        "対象期間（開始）": d.period_start or "—",
                        "対象期間（終了）": d.period_end or "—",
                        "PDF": "✅" if d.pdf_flag else "—",
                        "書類ID": d.doc_id,
                    }
                    for d in docs
                ]
            ).sort_values("提出日", ascending=False)

            st.dataframe(df.drop(columns=["書類ID"]), use_container_width=True, hide_index=True)

            st.divider()
            st.subheader("📥 PDF を表示・ダウンロード")
            doc_options = {
                f"{d.submit_date} {DOC_TYPE_CODES.get(d.doc_type_code, d.doc_type_code)} ({d.doc_id})": d
                for d in docs
                if d.pdf_flag
            }
            if not doc_options:
                st.info("PDF が利用可能な書類がありません。")
            else:
                selected_label = st.selectbox("書類を選択", options=list(doc_options.keys()))
                selected_doc = doc_options[selected_label]

                col_view, col_dl = st.columns(2)
                with col_view:
                    if st.button("PDF を取得", type="primary"):
                        with st.spinner("PDF をダウンロード中…"):
                            try:
                                pdf_bytes = client.get_pdf(selected_doc.doc_id)
                                st.download_button(
                                    label="📥 PDF をダウンロード",
                                    data=pdf_bytes,
                                    file_name=f"{selected_doc.doc_id}.pdf",
                                    mime="application/pdf",
                                    use_container_width=True,
                                )
                                st.success(f"PDF を取得しました（{len(pdf_bytes):,} bytes）")
                            except Exception as exc:
                                st.error(f"PDF 取得エラー: {exc}")

                with col_dl:
                    edinet_url = f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S1{selected_doc.doc_id[1:]}"
                    st.link_button(
                        "🌐 EDINET で開く",
                        url=edinet_url,
                        use_container_width=True,
                    )

# --- タブ3: 赤信号チェック ---
with tab_redflags:
    guide = get_guide(selected_strategy)
    st.subheader("🚩 赤信号チェックリスト")
    st.caption(
        "以下の項目は書類を読む際に特に注意すべき財務リスクのサインです。"
        "1つでも該当する場合は投資判断に慎重になることを推奨します。"
    )

    st.markdown("#### 全手法共通チェック")
    for rf in get_common_red_flags():
        with st.expander(f"🚩 {rf.label}"):
            st.write(rf.description)

    if guide and guide.red_flags:
        st.markdown(f"#### {guide.strategy_name} 固有のチェック")
        for rf in guide.red_flags:
            with st.expander(f"⚠️ {rf.label}"):
                st.write(rf.description)

    st.divider()
    st.markdown("#### ✅ 自分用チェックメモ")
    st.caption("読んだ箇所のメモを残せます（このセッション限り）。")

    if "reading_notes" not in st.session_state:
        st.session_state.reading_notes = ""

    notes = st.text_area(
        "メモ",
        value=st.session_state.reading_notes,
        height=150,
        placeholder="・CFは黒字で安定\n・継続企業注記なし\n・政策保有株の縮減計画あり（2027年まで50%削減目標）",
    )
    if notes != st.session_state.reading_notes:
        st.session_state.reading_notes = notes
        st.caption("メモを更新しました。")
