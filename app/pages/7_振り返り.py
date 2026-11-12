"""Phase 4: 振り返り — 投資理由と結果を比較し、自分の傾向を可視化する。"""

from __future__ import annotations

import datetime

import pandas as pd
import streamlit as st

st.set_page_config(page_title="振り返り", page_icon="🔍", layout="wide")

st.title("🔍 投資の振り返り")
st.caption("取引理由と結果を比較し、自分の投資行動の傾向を把握します。")

try:
    from core.paper.portfolio import PaperPortfolio
    from core.data.store import Store, DEFAULT_DB_PATH

    portfolio = PaperPortfolio()
    store = Store(DEFAULT_DB_PATH)
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


trades_df = portfolio.get_trades(limit=1000)

if trades_df.empty:
    st.info("取引履歴がありません。ペーパートレードページで売買を行ってから振り返りをしてください。")
    st.stop()

trades_df["trade_date"] = pd.to_datetime(trades_df["trade_date"])

# ---------------------------------------------------------------------------
# タブ構成
# ---------------------------------------------------------------------------
tab_pnl, tab_behavior, tab_detail = st.tabs(
    ["💰 損益分析", "🧠 行動傾向", "📖 取引詳細レビュー"]
)

# ===========================================================================
# タブ1: 損益分析
# ===========================================================================
with tab_pnl:
    st.subheader("💰 売買損益の分析")

    # 売り取引から実現損益を計算
    buys = trades_df[trades_df["side"] == "buy"].copy()
    sells = trades_df[trades_df["side"] == "sell"].copy()

    # コードごとの平均取得単価（簡易）
    avg_costs: dict[str, float] = {}
    for code, group in buys.groupby("code"):
        total_shares = group["shares"].sum()
        total_amount = group["amount"].sum()
        avg_costs[str(code)] = total_amount / total_shares if total_shares > 0 else 0

    # 実現損益
    realized_rows = []
    for _, row in sells.iterrows():
        code = str(row["code"])
        avg_cost = avg_costs.get(code, row["price"])
        pnl = int((row["price"] - avg_cost) * row["shares"])
        pnl_pct = (row["price"] - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0
        realized_rows.append({
            "取引日": row["trade_date"].date(),
            "コード": code,
            "株数": row["shares"],
            "売却価格": row["price"],
            "取得単価（推定）": avg_cost,
            "実現損益": pnl,
            "損益率(%)": round(pnl_pct, 2),
            "手法": STRATEGY_LABELS.get(str(row.get("strategy") or ""), "—"),
            "理由": str(row["reason"])[:60] + "…" if len(str(row["reason"])) > 60 else str(row["reason"]),
        })

    if realized_rows:
        realized_df = pd.DataFrame(realized_rows)
        total_pnl = realized_df["実現損益"].sum()
        win_count = (realized_df["実現損益"] > 0).sum()
        lose_count = (realized_df["実現損益"] <= 0).sum()
        win_rate = win_count / len(realized_df) * 100 if len(realized_df) > 0 else 0

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            color = "normal" if total_pnl >= 0 else "inverse"
            st.metric("実現損益合計", f"¥{total_pnl:+,}", delta_color=color)
        with c2:
            st.metric("勝率", f"{win_rate:.1f}%", f"{win_count}勝 {lose_count}敗")
        with c3:
            avg_win = realized_df[realized_df["実現損益"] > 0]["損益率(%)"].mean()
            st.metric("平均利益率", f"{avg_win:+.1f}%" if win_count > 0 else "—")
        with c4:
            avg_lose = realized_df[realized_df["実現損益"] <= 0]["損益率(%)"].mean()
            st.metric("平均損失率", f"{avg_lose:+.1f}%" if lose_count > 0 else "—")

        st.divider()
        st.dataframe(
            realized_df.sort_values("取引日", ascending=False),
            use_container_width=True, hide_index=True,
        )

        # 手法別損益
        if len(realized_df["手法"].unique()) > 1:
            st.subheader("手法別 実現損益")
            strategy_pnl = realized_df.groupby("手法")["実現損益"].sum().reset_index()
            st.bar_chart(strategy_pnl.set_index("手法"))
    else:
        st.info("売却済みの取引がありません。")

    # 未実現損益
    st.divider()
    st.subheader("含み損益（現在の保有銘柄）")
    positions = portfolio.get_positions()
    if positions:
        unrealized_rows = []
        for p in positions:
            price = get_latest_price(p.code)
            if price:
                p.current_price = price
            unrealized_rows.append({
                "コード": p.code,
                "株数": p.shares,
                "取得単価": p.avg_cost,
                "現在値": p.current_price if p.current_price else "—",
                "含み損益": f"{p.unrealized_pnl:+,}円" if p.current_price else "—",
                "含み損益率": f"{p.unrealized_pnl_pct:+.1f}%" if p.current_price else "—",
            })
        st.dataframe(pd.DataFrame(unrealized_rows), use_container_width=True, hide_index=True)
    else:
        st.info("現在の保有銘柄はありません。")

# ===========================================================================
# タブ2: 行動傾向
# ===========================================================================
with tab_behavior:
    st.subheader("🧠 投資行動の傾向分析")

    # 手法別取引数
    strategy_counts = trades_df["strategy"].fillna("未設定").value_counts().reset_index()
    strategy_counts.columns = ["手法", "取引数"]
    strategy_counts["手法"] = strategy_counts["手法"].map(
        lambda s: STRATEGY_LABELS.get(s, s)
    )

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("#### 手法別取引数")
        st.bar_chart(strategy_counts.set_index("手法"))

    with col2:
        st.markdown("#### 買い vs 売り")
        side_counts = trades_df["side"].value_counts().reset_index()
        side_counts.columns = ["売買", "件数"]
        side_counts["売買"] = side_counts["売買"].map({"buy": "買い", "sell": "売り"})
        st.bar_chart(side_counts.set_index("売買"))

    # 月別取引数
    st.divider()
    st.markdown("#### 月別取引数")
    trades_df["月"] = trades_df["trade_date"].dt.to_period("M").astype(str)
    monthly_counts = trades_df.groupby("月").size().reset_index(name="取引数")
    st.bar_chart(monthly_counts.set_index("月"))

    # 傾向チェック
    st.divider()
    st.markdown("#### 🔎 自動チェック")

    checks = []

    # 損切り傾向
    if realized_rows:
        realized_df2 = pd.DataFrame(realized_rows)
        loss_trades = realized_df2[realized_df2["損益率(%)"] < -10]
        if len(loss_trades) > 0:
            avg_loss_pct = loss_trades["損益率(%)"].mean()
            checks.append(("⚠️", f"損失が-10%超の取引が {len(loss_trades)} 件あります（平均 {avg_loss_pct:.1f}%）。損切りルールを見直しましょう。"))

        profit_trades = realized_df2[realized_df2["損益率(%)"] > 0]
        if len(profit_trades) > 0 and len(loss_trades) > 0:
            avg_profit = profit_trades["損益率(%)"].mean()
            avg_loss2 = abs(loss_trades["損益率(%)"].mean())
            if avg_profit < avg_loss2:
                checks.append(("⚠️", f"平均利益率（{avg_profit:.1f}%）が平均損失率（{avg_loss2:.1f}%）より小さいです。利益を早めに確定しすぎている可能性があります。"))

    if len(trades_df) > 20:
        checks.append(("ℹ️", f"取引件数が {len(trades_df)} 件。取引頻度が高い場合は、各取引の根拠を再確認してください。"))

    buy_count = len(trades_df[trades_df["side"] == "buy"])
    sell_count = len(trades_df[trades_df["side"] == "sell"])
    if buy_count > sell_count * 2:
        checks.append(("ℹ️", "買い取引が売り取引の2倍以上あります。ポジションの整理を検討してください。"))

    if not checks:
        checks.append(("✅", "目立った傾向は検出されませんでした。"))

    for icon, msg in checks:
        st.markdown(f"{icon} {msg}")

# ===========================================================================
# タブ3: 取引詳細レビュー
# ===========================================================================
with tab_detail:
    st.subheader("📖 取引詳細レビュー")
    st.caption("個々の取引の投資理由を振り返り、判断の質を評価します。")

    # 取引選択
    display_trades = trades_df.copy()
    display_trades["ラベル"] = (
        display_trades["trade_date"].dt.strftime("%Y-%m-%d") + " " +
        display_trades["code"] + " " +
        display_trades["side"].map({"buy": "買", "sell": "売"}) + " " +
        display_trades["shares"].astype(str) + "株"
    )

    selected_label = st.selectbox("取引を選択", options=display_trades["ラベル"].tolist())
    selected_row = display_trades[display_trades["ラベル"] == selected_label].iloc[0]

    with st.container(border=True):
        c1, c2 = st.columns(2)
        with c1:
            st.write(f"**銘柄:** {selected_row['code']}")
            st.write(f"**売買:** {'🟢 買い' if selected_row['side'] == 'buy' else '🔴 売り'}")
            st.write(f"**取引日:** {selected_row['trade_date'].date()}")
            st.write(f"**約定:** {selected_row['shares']:,}株 × ¥{selected_row['price']:,.0f} = ¥{selected_row['amount']:,}")
        with c2:
            st.write(f"**手法:** {STRATEGY_LABELS.get(str(selected_row.get('strategy') or ''), '—')}")
            price_now = get_latest_price(str(selected_row["code"]))
            if price_now and selected_row["side"] == "buy":
                pnl_pct = (price_now - selected_row["price"]) / selected_row["price"] * 100
                st.metric("現在の損益率", f"{pnl_pct:+.1f}%", f"現在値 ¥{price_now:,.0f}")

        st.write("**投資理由:**")
        st.markdown(f"> {selected_row['reason']}")

    # 自己評価メモ
    st.markdown("#### ✍️ 振り返りメモ")
    memo_key = f"retro_{selected_row['trade_id']}"
    if memo_key not in st.session_state:
        st.session_state[memo_key] = ""

    retro_note = st.text_area(
        "この取引を振り返って",
        value=st.session_state[memo_key],
        height=120,
        placeholder="・理由は事前に持っていた情報か？\n・感情的な判断はなかったか？\n・撤退条件は明確だったか？\n・次回に活かせる教訓は？",
        key=f"retro_input_{selected_row['trade_id']}",
    )
    if retro_note != st.session_state[memo_key]:
        st.session_state[memo_key] = retro_note
        st.caption("メモを更新しました（このセッション限り）。")
