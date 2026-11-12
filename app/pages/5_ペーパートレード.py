"""Phase 4: ペーパートレード — 仮想資金での売買と損益記録。

機能:
- 口座サマリー（資金・NAV・損益）
- 保有銘柄一覧（含み損益・評価額）
- 売買注文（投資理由の入力必須）
- 取引履歴
- NAV 推移チャート
"""

from __future__ import annotations

import datetime
import os

import pandas as pd
import streamlit as st

st.set_page_config(page_title="ペーパートレード", page_icon="📊", layout="wide")

st.title("📊 ペーパートレード")
st.caption("仮想資金で売買し、規律ある投資判断を練習します。")
st.warning("⚠️ これは仮想取引です。実際の投資推奨ではありません。", icon="⚠️")

try:
    from core.paper.portfolio import DEFAULT_INITIAL_CASH, PaperPortfolio
    from core.data.store import Store, DEFAULT_DB_PATH
    portfolio = PaperPortfolio()
    store = Store(DEFAULT_DB_PATH)
    STORE_AVAILABLE = True
except Exception as e:
    st.error(f"モジュールのロードに失敗しました: {e}")
    STORE_AVAILABLE = False
    st.stop()

STRATEGY_LABELS: dict[str, str] = {
    "": "（手法を選択）",
    "buffett": "バフェット流",
    "graham": "グレアム",
    "greenblatt": "グリーンブラット",
    "lynch": "リンチ（PEG）",
    "high_dividend": "高配当・累進配当",
    "tse_reform": "東証改革テーマ",
    "momentum": "モメンタム",
    "other": "その他",
}

# ---------------------------------------------------------------------------
# 口座の取得・初期化
# ---------------------------------------------------------------------------
acct = portfolio.get_or_create_account()


def _refresh_account():
    return portfolio.get_or_create_account()


# ---------------------------------------------------------------------------
# 現在価格の取得ヘルパー（DuckDB から終値を取得）
# ---------------------------------------------------------------------------
def get_latest_price(code: str) -> float | None:
    try:
        with store._conn() as conn:
            row = conn.execute(
                """SELECT adj_close FROM daily_quotes
                   WHERE code = ? ORDER BY trade_date DESC LIMIT 1""",
                [code],
            ).fetchone()
        return float(row[0]) if row else None
    except Exception:
        return None


def get_stock_name(code: str) -> str:
    try:
        with store._conn() as conn:
            row = conn.execute(
                """SELECT name FROM listed_info
                   WHERE code = ? ORDER BY as_of DESC LIMIT 1""",
                [code],
            ).fetchone()
        return row[0] if row else code
    except Exception:
        return code


# ---------------------------------------------------------------------------
# タブ構成
# ---------------------------------------------------------------------------
tab_summary, tab_order, tab_history, tab_chart = st.tabs(
    ["📈 口座サマリー", "🛒 売買注文", "📋 取引履歴", "📉 NAV 推移"]
)

# ===========================================================================
# タブ1: 口座サマリー
# ===========================================================================
with tab_summary:
    acct = _refresh_account()
    positions = portfolio.get_positions()

    for p in positions:
        price = get_latest_price(p.code)
        if price:
            p.current_price = price

    stocks_value = sum(p.market_value for p in positions)
    nav = acct.cash + stocks_value
    total_pnl = nav - acct.initial_cash
    total_pnl_pct = total_pnl / acct.initial_cash * 100

    # --- KPI カード ---
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("NAV（純資産）", f"¥{nav:,.0f}", f"{total_pnl:+,.0f}円 ({total_pnl_pct:+.2f}%)")
    with c2:
        st.metric("現金", f"¥{acct.cash:,.0f}")
    with c3:
        st.metric("株式評価額", f"¥{stocks_value:,.0f}")
    with c4:
        st.metric("初期資金", f"¥{acct.initial_cash:,.0f}")

    st.divider()

    # --- 保有銘柄一覧 ---
    st.subheader("保有銘柄")
    if not positions:
        st.info("現在の保有銘柄はありません。「売買注文」タブから銘柄を購入できます。")
    else:
        rows = []
        for p in positions:
            name = get_stock_name(p.code)
            rows.append({
                "コード": p.code,
                "銘柄名": name,
                "株数": f"{p.shares:,}",
                "取得単価": f"¥{p.avg_cost:,.0f}",
                "現在値": f"¥{p.current_price:,.0f}" if p.current_price else "—",
                "評価額": f"¥{p.market_value:,}" if p.current_price else "—",
                "含み損益": f"{p.unrealized_pnl:+,}円 ({p.unrealized_pnl_pct:+.1f}%)" if p.current_price else "—",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        # NAV を記録
        today = datetime.date.today()
        portfolio.save_nav(today, positions, acct.cash)

    st.divider()
    with st.expander("⚙️ 口座リセット（全取引・ポジションを消去）"):
        new_cash = st.number_input(
            "初期資金（円）",
            min_value=1_000_000,
            max_value=100_000_000,
            value=DEFAULT_INITIAL_CASH,
            step=1_000_000,
            format="%d",
        )
        if st.button("🔄 口座をリセット", type="secondary"):
            portfolio.reset_account(initial_cash=int(new_cash))
            st.success("口座をリセットしました。")
            st.rerun()

# ===========================================================================
# タブ2: 売買注文
# ===========================================================================
with tab_order:
    st.subheader("🛒 売買注文")

    col_l, col_r = st.columns(2)

    with col_l:
        st.markdown("#### 注文内容")
        order_code = st.text_input(
            "証券コード（5桁）",
            placeholder="例: 72030",
            help="J-Quants V2 形式の5桁コード",
        )
        order_side = st.radio("売買", ["買い", "売り"], horizontal=True)
        order_shares = st.number_input("株数", min_value=1, value=100, step=100)
        order_price_mode = st.radio(
            "価格指定",
            ["最新終値を使用", "手入力"],
            horizontal=True,
        )

        price_val: float | None = None
        if order_code.strip():
            latest = get_latest_price(order_code.strip())
            if order_price_mode == "最新終値を使用":
                if latest:
                    price_val = latest
                    st.info(f"最新終値: ¥{latest:,.0f}")
                else:
                    st.warning("価格データが見つかりません。手入力を選択してください。")
            else:
                price_val = st.number_input(
                    "約定価格（円）",
                    min_value=1.0,
                    value=float(latest) if latest else 1000.0,
                    step=1.0,
                )

    with col_r:
        st.markdown("#### 投資理由（必須）")
        order_reason = st.text_area(
            "なぜ今この銘柄を売買するのか",
            height=160,
            placeholder=(
                "例:\n"
                "・ROE 15%超が10年継続、競争優位（バフェット基準を満たす）\n"
                "・PBR 0.8倍で清算価値より割安\n"
                "・自社株買い発表でカタリスト発生\n"
                "・撤退条件: 四半期連続営業赤字"
            ),
        )
        order_strategy = st.selectbox(
            "手法タグ（任意）",
            options=list(STRATEGY_LABELS.keys()),
            format_func=lambda k: STRATEGY_LABELS[k],
        )
        order_date = st.date_input(
            "取引日",
            value=datetime.date.today(),
            max_value=datetime.date.today(),
        )

    # 注文確認・発注
    st.divider()
    if order_code.strip() and price_val and order_reason.strip():
        amount = int(order_shares * price_val)
        acct_now = portfolio.get_account()
        side_str = "買い" if order_side == "買い" else "売り"

        st.markdown(
            f"**注文内容確認:** {order_code} を {order_shares:,}株 × ¥{price_val:,.0f} = **¥{amount:,}** で{side_str}"
        )
        if order_side == "買い" and acct_now and acct_now.cash < amount:
            st.error(f"資金不足: 残高 ¥{acct_now.cash:,} < 注文額 ¥{amount:,}")
        else:
            if st.button(f"✅ {side_str}注文を執行する", type="primary"):
                try:
                    if order_side == "買い":
                        trade = portfolio.buy(
                            code=order_code.strip(),
                            shares=int(order_shares),
                            price=price_val,
                            reason=order_reason,
                            trade_date=order_date,
                            strategy=order_strategy or None,
                        )
                    else:
                        trade = portfolio.sell(
                            code=order_code.strip(),
                            shares=int(order_shares),
                            price=price_val,
                            reason=order_reason,
                            trade_date=order_date,
                            strategy=order_strategy or None,
                        )
                    st.success(
                        f"約定しました！ {side_str} {trade.code} {trade.shares:,}株 "
                        f"@¥{trade.price:,.0f} = ¥{trade.amount:,}"
                    )
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
    else:
        st.info("証券コード・価格・投資理由をすべて入力すると注文ボタンが表示されます。")

# ===========================================================================
# タブ3: 取引履歴
# ===========================================================================
with tab_history:
    st.subheader("📋 取引履歴")
    trades_df = portfolio.get_trades(limit=200)

    if trades_df.empty:
        st.info("取引履歴がありません。")
    else:
        # 表示用に整形
        display_df = trades_df.copy()
        display_df["売買"] = display_df["side"].map({"buy": "🟢 買い", "sell": "🔴 売り"})
        display_df["約定金額"] = display_df["amount"].apply(lambda x: f"¥{x:,}")
        display_df["単価"] = display_df["price"].apply(lambda x: f"¥{x:,.0f}")
        display_df["投資理由"] = display_df["reason"].str[:50] + "…"

        st.dataframe(
            display_df[["trade_date", "code", "売買", "shares", "単価", "約定金額", "strategy", "投資理由"]].rename(
                columns={
                    "trade_date": "取引日",
                    "code": "コード",
                    "shares": "株数",
                    "strategy": "手法",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )

        st.divider()
        st.subheader("取引詳細")
        trade_ids = trades_df["trade_id"].tolist()
        selected_id = st.selectbox(
            "取引を選択して投資理由を確認",
            options=trade_ids,
            format_func=lambda tid: (
                trades_df.loc[trades_df["trade_id"] == tid].iloc[0][["trade_date", "code", "side"]]
                .to_list().__repr__().strip("[]").replace("'", "")
            ),
        )
        if selected_id:
            row = trades_df.loc[trades_df["trade_id"] == selected_id].iloc[0]
            with st.container(border=True):
                c1, c2 = st.columns(2)
                with c1:
                    st.write(f"**銘柄コード:** {row['code']}")
                    st.write(f"**売買:** {'買い' if row['side'] == 'buy' else '売り'}")
                    st.write(f"**取引日:** {row['trade_date']}")
                    st.write(f"**約定:** {row['shares']:,}株 × ¥{row['price']:,.0f}")
                with c2:
                    st.write(f"**手法:** {STRATEGY_LABELS.get(row['strategy'] or '', '—')}")
                st.write("**投資理由:**")
                st.markdown(f"> {row['reason']}")

# ===========================================================================
# タブ4: NAV 推移
# ===========================================================================
with tab_chart:
    st.subheader("📉 NAV 推移")

    nav_df = portfolio.get_nav_history()
    if nav_df.empty:
        st.info(
            "NAV 履歴がありません。保有銘柄がある状態で「口座サマリー」タブを開くと自動記録されます。"
        )
    else:
        nav_df["nav_date"] = pd.to_datetime(nav_df["nav_date"])
        nav_df["リターン(%)"] = (nav_df["nav"] / nav_df["nav"].iloc[0] - 1) * 100

        col_m, col_e = st.columns(2)
        with col_m:
            st.metric(
                "累積リターン",
                f"{nav_df['リターン(%)'].iloc[-1]:+.2f}%",
                f"NAV ¥{nav_df['nav'].iloc[-1]:,}",
            )
        with col_e:
            days = (nav_df["nav_date"].iloc[-1] - nav_df["nav_date"].iloc[0]).days
            st.metric("運用日数", f"{days} 日")

        # NAV チャート
        st.line_chart(
            nav_df.set_index("nav_date")[["nav"]].rename(columns={"nav": "NAV（円）"}),
            use_container_width=True,
        )

        # 内訳（現金 vs 株式）
        st.subheader("資産内訳の推移")
        st.area_chart(
            nav_df.set_index("nav_date")[["cash", "stocks_value"]].rename(
                columns={"cash": "現金", "stocks_value": "株式評価額"}
            ),
            use_container_width=True,
        )

        with st.expander("データを表示"):
            show_df = nav_df.copy()
            show_df["nav_date"] = show_df["nav_date"].dt.strftime("%Y-%m-%d")
            show_df["NAV"] = show_df["nav"].apply(lambda x: f"¥{x:,}")
            show_df["現金"] = show_df["cash"].apply(lambda x: f"¥{x:,}")
            show_df["株式"] = show_df["stocks_value"].apply(lambda x: f"¥{x:,}")
            st.dataframe(
                show_df[["nav_date", "NAV", "現金", "株式", "リターン(%)"]].rename(
                    columns={"nav_date": "日付", "リターン(%)": "リターン"}
                ),
                use_container_width=True,
                hide_index=True,
            )
