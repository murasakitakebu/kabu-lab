"""Phase 4: 自己資本投資部モード。

機能:
- 投資メモ作成（テーゼ・カタリスト・リスク・撤退条件）
- AI投資委員会による審査（反論・質問 → 承認後に売買）
- 月次運用報告書の自動生成
"""

from __future__ import annotations

import datetime
import uuid

import pandas as pd
import streamlit as st

st.set_page_config(page_title="投資部モード", page_icon="🏦", layout="wide")

st.title("🏦 自己資本投資部モード")
st.caption("投資メモ → AI委員会審査 → 承認後に売買。規律ある意思決定を練習します。")
st.warning("⚠️ 本ツールの出力は学習・研究目的です。投資助言ではありません。", icon="⚠️")

# ---------------------------------------------------------------------------
# imports
# ---------------------------------------------------------------------------
try:
    from core.paper.investment_memo import InvestmentMemo, MemoStore
    from core.paper.portfolio import PaperPortfolio
    from core.paper.committee import CommitteeRequest, stream_review, stream_monthly_report
    from core.data.store import Store, DEFAULT_DB_PATH
    from zoneinfo import ZoneInfo

    JST = ZoneInfo("Asia/Tokyo")
    memo_store = MemoStore()
    portfolio = PaperPortfolio()
    store = Store(DEFAULT_DB_PATH)

    import os
    AI_AVAILABLE = bool(os.environ.get("ANTHROPIC_API_KEY"))
except Exception as e:
    st.error(f"モジュールのロードに失敗しました: {e}")
    st.stop()

STRATEGY_LABELS: dict[str, str] = {
    "buffett": "バフェット流", "graham": "グレアム", "greenblatt": "グリーンブラット",
    "lynch": "リンチ（PEG）", "high_dividend": "高配当・累進配当",
    "tse_reform": "東証改革テーマ", "momentum": "モメンタム", "other": "その他",
}

def get_latest_price(code: str) -> float | None:
    try:
        with store._conn() as conn:
            row = conn.execute(
                "SELECT adj_close FROM daily_quotes WHERE code = ? ORDER BY trade_date DESC LIMIT 1",
                [code],
            ).fetchone()
        return float(row[0]) if row else None
    except Exception:
        return None

# ---------------------------------------------------------------------------
# タブ構成
# ---------------------------------------------------------------------------
tab_memo, tab_committee, tab_report = st.tabs(
    ["📝 投資メモ", "🏛️ AI投資委員会", "📊 月次報告書"]
)

# ===========================================================================
# タブ1: 投資メモ
# ===========================================================================
with tab_memo:
    col_form, col_list = st.columns([1, 1])

    with col_form:
        st.subheader("📝 新規投資メモ作成")

        code_input = st.text_input("証券コード（5桁）", placeholder="例: 72030", key="memo_code")
        company_input = st.text_input("会社名", placeholder="例: トヨタ自動車", key="memo_company")

        strategy_input = st.selectbox(
            "投資手法", options=list(STRATEGY_LABELS.keys()),
            format_func=lambda k: STRATEGY_LABELS[k], key="memo_strategy"
        )
        target_price_input = st.number_input(
            "目標株価（円、任意）", min_value=0.0, value=0.0, step=100.0, key="memo_target"
        )

        st.markdown("#### 投資テーゼ")
        thesis_input = st.text_area(
            "なぜこの銘柄に投資するのか（ビジネスモデル・競争優位・割安の根拠など）",
            height=120,
            placeholder="例: ROE15%超が10年継続する高品質企業。ブランド力による価格支配力があり、フリーCFが潤沢。PER15倍で妥当水準以下。",
            key="memo_thesis"
        )

        st.markdown("#### カタリスト")
        catalyst_input = st.text_area(
            "いつ・何が起きて株価が上昇するか",
            height=100,
            placeholder="例: 2025年3月期に海外売上比率が50%超となり、為替メリットが顕在化する見込み。自社株買い発表も期待。",
            key="memo_catalyst"
        )

        st.markdown("#### リスク")
        risks_input = st.text_area(
            "下振れシナリオ・懸念材料",
            height=100,
            placeholder="例: 円高進行による海外売上の目減り。主要顧客の設備投資抑制リスク。後継者問題。",
            key="memo_risks"
        )

        st.markdown("#### 撤退条件")
        exit_input = st.text_area(
            "何があったら売るか（具体的に）",
            height=100,
            placeholder="例: ・2四半期連続で営業利益が前年割れ\n・ROEが10%を下回る\n・テーゼの前提（ブランド力）が崩れる事象",
            key="memo_exit"
        )

        can_save = all([
            code_input.strip(), thesis_input.strip(),
            catalyst_input.strip(), risks_input.strip(), exit_input.strip()
        ])

        if st.button("💾 メモを保存", type="primary", disabled=not can_save):
            now = datetime.datetime.now(JST)
            memo_id = f"memo_{uuid.uuid4().hex[:8]}"
            memo = InvestmentMemo(
                memo_id=memo_id,
                account_id="default",
                code=code_input.strip(),
                company_name=company_input.strip() or None,
                thesis=thesis_input,
                catalyst=catalyst_input,
                risks=risks_input,
                exit_cond=exit_input,
                target_price=target_price_input if target_price_input > 0 else None,
                strategy=strategy_input,
                status="draft",
                created_at=now,
                updated_at=now,
            )
            memo_store.save_memo(memo)
            st.success(f"メモを保存しました（ID: {memo_id}）。「AI投資委員会」タブで審査を受けてください。")
            st.rerun()

        if not can_save and code_input:
            st.info("すべての項目を入力するとボタンが有効になります。")

    with col_list:
        st.subheader("📋 投資メモ一覧")
        memos_df = memo_store.list_memos()

        if memos_df.empty:
            st.info("投資メモがありません。左側のフォームから作成してください。")
        else:
            status_emoji = {"draft": "📝", "approved": "✅", "rejected": "❌", "closed": "🔒"}
            display_df = memos_df.copy()
            display_df["状態"] = display_df["status"].map(lambda s: f"{status_emoji.get(s,'?')} {s}")
            display_df["テーゼ（抜粋）"] = display_df["thesis"].str[:40] + "…"
            st.dataframe(
                display_df[["code", "company_name", "strategy", "状態", "テーゼ（抜粋）", "created_at"]].rename(
                    columns={"code": "コード", "company_name": "会社名",
                             "strategy": "手法", "created_at": "作成日"}
                ),
                use_container_width=True, hide_index=True,
            )

# ===========================================================================
# タブ2: AI投資委員会
# ===========================================================================
with tab_committee:
    st.subheader("🏛️ AI投資委員会")
    st.caption("投資メモを提出すると、AIが厳格な委員長として審査・質問を行います。")

    if not AI_AVAILABLE:
        st.warning("ANTHROPIC_API_KEY が設定されていません。", icon="🔑")
        st.stop()

    memos_df = memo_store.list_memos()
    if memos_df.empty:
        st.info("「投資メモ」タブでメモを作成してから審査を依頼してください。")
    else:
        # メモ選択
        memo_options = {
            f"{row['code']} {row['company_name'] or ''} ({row['status']}) — {row['memo_id']}": row['memo_id']
            for _, row in memos_df.iterrows()
        }
        selected_label = st.selectbox("審査するメモを選択", options=list(memo_options.keys()))
        selected_memo_id = memo_options[selected_label]
        selected_memo = memo_store.get_memo(selected_memo_id)

        if selected_memo:
            # メモ内容の表示
            with st.expander("📋 投資メモの内容", expanded=True):
                c1, c2 = st.columns(2)
                with c1:
                    st.write(f"**銘柄:** {selected_memo.code} {selected_memo.company_name or ''}")
                    st.write(f"**手法:** {STRATEGY_LABELS.get(selected_memo.strategy or '', '—')}")
                    if selected_memo.target_price:
                        st.write(f"**目標株価:** ¥{selected_memo.target_price:,.0f}")
                    price = get_latest_price(selected_memo.code)
                    if price:
                        st.write(f"**現在値:** ¥{price:,.0f}")
                with c2:
                    st.write(f"**状態:** {selected_memo.status}")

                st.markdown(f"**投資テーゼ:**\n> {selected_memo.thesis}")
                st.markdown(f"**カタリスト:**\n> {selected_memo.catalyst}")
                st.markdown(f"**リスク:**\n> {selected_memo.risks}")
                st.markdown(f"**撤退条件:**\n> {selected_memo.exit_cond}")

            st.divider()

            # 審査ボタン
            if st.button("🏛️ 委員会に審査を依頼する", type="primary"):
                req = CommitteeRequest(
                    memo=selected_memo,
                    strategy_name=STRATEGY_LABELS.get(selected_memo.strategy or "", ""),
                )

                st.markdown("### 委員会審査結果")
                result_placeholder = st.empty()
                full_text = ""

                try:
                    with st.spinner("委員会が審査中…"):
                        for chunk in stream_review(req):
                            full_text += chunk
                            result_placeholder.markdown(full_text + "▌")

                    result_placeholder.markdown(full_text)

                    # セッションに保存
                    st.session_state[f"review_{selected_memo_id}"] = full_text

                except Exception as exc:
                    st.error(f"AI審査エラー: {exc}")

            # 既存の審査結果があれば表示
            elif f"review_{selected_memo_id}" in st.session_state:
                st.markdown("### 前回の委員会審査結果")
                st.markdown(st.session_state[f"review_{selected_memo_id}"])

            # 承認 / 却下ボタン
            st.divider()
            st.markdown("### 委員会決定")
            col_a, col_r, col_c = st.columns(3)
            with col_a:
                if st.button("✅ 承認（売買を許可）", type="primary", use_container_width=True):
                    memo_store.update_status(selected_memo_id, "approved")
                    st.success("承認しました。ペーパートレードページから売買できます。")
                    st.rerun()
            with col_r:
                if st.button("❌ 却下（見送り）", type="secondary", use_container_width=True):
                    memo_store.update_status(selected_memo_id, "rejected")
                    st.warning("却下しました。メモを修正して再審査できます。")
                    st.rerun()
            with col_c:
                if st.button("🔒 クローズ（終了）", type="secondary", use_container_width=True):
                    memo_store.update_status(selected_memo_id, "closed")
                    st.info("クローズしました。")
                    st.rerun()

# ===========================================================================
# タブ3: 月次運用報告書
# ===========================================================================
with tab_report:
    st.subheader("📊 月次運用報告書")
    st.caption("今月の取引・パフォーマンスをもとに、AIがファンドマネージャー目線で報告書を生成します。")

    if not AI_AVAILABLE:
        st.warning("ANTHROPIC_API_KEY が設定されていません。", icon="🔑")
        st.stop()

    # 対象月の選択
    today = datetime.date.today()
    report_month = st.date_input(
        "対象月（月初日を選択）",
        value=today.replace(day=1),
        max_value=today,
    )
    month_start = report_month.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1, day=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1, day=1)

    # データ収集
    acct = portfolio.get_account()
    trades_df = portfolio.get_trades(limit=500)
    nav_df = portfolio.get_nav_history()
    positions = portfolio.get_positions()

    if acct is None:
        st.info("口座がありません。ペーパートレードページで口座を作成してください。")
        st.stop()

    # 当月取引フィルタ
    if not trades_df.empty:
        trades_df["trade_date"] = pd.to_datetime(trades_df["trade_date"])
        month_trades = trades_df[
            (trades_df["trade_date"] >= pd.Timestamp(month_start)) &
            (trades_df["trade_date"] < pd.Timestamp(month_end))
        ]
    else:
        month_trades = pd.DataFrame()

    # 当月NAV
    if not nav_df.empty:
        nav_df["nav_date"] = pd.to_datetime(nav_df["nav_date"])
        month_nav = nav_df[
            (nav_df["nav_date"] >= pd.Timestamp(month_start)) &
            (nav_df["nav_date"] < pd.Timestamp(month_end))
        ]
    else:
        month_nav = pd.DataFrame()

    # サマリー表示
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("当月取引件数", f"{len(month_trades)} 件")
    with col2:
        nav_start = int(month_nav["nav"].iloc[0]) if not month_nav.empty else acct.initial_cash
        nav_end = int(month_nav["nav"].iloc[-1]) if not month_nav.empty else acct.cash
        monthly_ret = (nav_end - nav_start) / nav_start * 100 if nav_start > 0 else 0
        st.metric("月間リターン", f"{monthly_ret:+.2f}%")
    with col3:
        st.metric("保有銘柄数", f"{len(positions)} 銘柄")

    st.divider()

    if st.button("📝 月次報告書を生成する", type="primary"):
        account_summary = {
            "口座名": acct.name,
            "初期資金": acct.initial_cash,
            "現金": acct.cash,
            "NAV（当月末）": nav_end,
            "月間リターン": f"{monthly_ret:+.2f}%",
            "保有銘柄数": len(positions),
        }

        trades_list = month_trades[
            ["trade_date", "code", "side", "shares", "price", "amount", "reason", "strategy"]
        ].to_dict("records") if not month_trades.empty else []

        nav_list = month_nav[["nav_date", "nav", "cash", "stocks_value"]].to_dict("records") \
            if not month_nav.empty else []

        st.markdown(f"## {month_start.strftime('%Y年%m月')} 月次運用報告書")
        result_placeholder = st.empty()
        full_text = ""

        try:
            with st.spinner("報告書を生成中…"):
                for chunk in stream_monthly_report(account_summary, trades_list, nav_list):
                    full_text += chunk
                    result_placeholder.markdown(full_text + "▌")

            result_placeholder.markdown(full_text)
            st.success("報告書が完成しました。")

            st.download_button(
                label="📥 報告書をテキストでダウンロード",
                data=full_text,
                file_name=f"monthly_report_{month_start.strftime('%Y%m')}.md",
                mime="text/markdown",
            )

        except Exception as exc:
            st.error(f"報告書生成エラー: {exc}")
