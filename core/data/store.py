"""DuckDB への保存と読み出し。

J-Quants V2 の DataFrame（短縮フィールド名・文字列混じり）を、このプロジェクトの
スキーマ（snake_case・型付き）に変換して upsert する。変換は純粋関数 `to_*` に分け、
DB 書き込みは `Store` が担う。

- 価格・指数：DOUBLE（調整後価格は小数になるため）
- 財務の金額：BIGINT（円）、1株あたりの値・比率：DECIMAL(18, 4)
- 財務・配当は元レコードを `raw`（JSON）にも保存し、後のフェーズで項目を足せるようにする
- 上場銘柄一覧は取得日ごとのスナップショットで持つ（上場廃止銘柄を残すため）
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

from core.data.jquants import normalize_code

JST = ZoneInfo("Asia/Tokyo")

#: 既定の DB パス（プロジェクトルートの data/）
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "kabulab.duckdb"

#: J-Quants の TOPIX 指数コード
TOPIX_CODE = "0000"

SCHEMA_VERSION = 1

Kind = Literal["str", "code", "date", "int", "dec", "float"]

# --- スキーマ -------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR
);

-- 上場銘柄一覧（取得日ごとのスナップショット）
CREATE TABLE IF NOT EXISTS listed_info (
    as_of         DATE    NOT NULL,
    code          VARCHAR NOT NULL,
    name          VARCHAR,
    name_en       VARCHAR,
    sector17      VARCHAR,
    sector17_name VARCHAR,
    sector33      VARCHAR,
    sector33_name VARCHAR,
    scale_cat     VARCHAR,
    market        VARCHAR,
    market_name   VARCHAR,
    margin        VARCHAR,
    margin_name   VARCHAR,
    PRIMARY KEY (as_of, code)
);

-- 株価四本値（日足）。未調整値と分割調整後の値を両方持つ
CREATE TABLE IF NOT EXISTS daily_bars (
    code        VARCHAR NOT NULL,
    date        DATE    NOT NULL,
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,
    upper_limit VARCHAR,
    lower_limit VARCHAR,
    volume      DOUBLE,
    turnover    DOUBLE,
    adj_factor  DOUBLE,
    adj_open    DOUBLE,
    adj_high    DOUBLE,
    adj_low     DOUBLE,
    adj_close   DOUBLE,
    adj_volume  DOUBLE,
    PRIMARY KEY (code, date)
);

-- 指数四本値（TOPIX は index_code = '0000'）
CREATE TABLE IF NOT EXISTS index_bars (
    index_code VARCHAR NOT NULL,
    date       DATE    NOT NULL,
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE,
    PRIMARY KEY (index_code, date)
);

-- 決算短信サマリ。ルックアヘッド防止のため disclosed_date を必ず持つ
CREATE TABLE IF NOT EXISTS fin_summary (
    disc_no             VARCHAR PRIMARY KEY,
    code                VARCHAR NOT NULL,
    disclosed_date      DATE    NOT NULL,
    disclosed_time      VARCHAR,
    doc_type            VARCHAR,
    period_type         VARCHAR,
    period_start        DATE,
    period_end          DATE,
    fy_start            DATE,
    fy_end              DATE,
    sales               BIGINT,
    operating_profit    BIGINT,
    ordinary_profit     BIGINT,
    net_profit          BIGINT,
    eps                 DECIMAL(18, 4),
    diluted_eps         DECIMAL(18, 4),
    total_assets        BIGINT,
    equity              BIGINT,
    equity_ratio        DECIMAL(18, 4),
    bps                 DECIMAL(18, 4),
    cfo                 BIGINT,
    cfi                 BIGINT,
    cff                 BIGINT,
    cash_eq             BIGINT,
    div_annual          DECIMAL(18, 4),
    payout_ratio        DECIMAL(18, 4),
    f_div_annual        DECIMAL(18, 4),
    f_sales             BIGINT,
    f_operating_profit  BIGINT,
    f_ordinary_profit   BIGINT,
    f_net_profit        BIGINT,
    f_eps               DECIMAL(18, 4),
    shares_outstanding  BIGINT,
    treasury_shares     BIGINT,
    avg_shares          BIGINT,
    raw                 JSON
);

-- 配当金情報（1 通知 = 1 行。訂正は ca_ref_no で元通知を参照）
CREATE TABLE IF NOT EXISTS dividends (
    ref_no              VARCHAR PRIMARY KEY,
    code                VARCHAR NOT NULL,
    pub_date            DATE    NOT NULL,
    pub_time            VARCHAR,
    status_code         VARCHAR,
    board_date          DATE,
    interim_final_code  VARCHAR,
    forecast_result_code VARCHAR,
    interim_final_term  VARCHAR,
    div_rate            DECIMAL(18, 4),
    record_date         DATE,
    ex_date             DATE,
    actual_record_date  DATE,
    pay_date            DATE,
    ca_ref_no           VARCHAR,
    comm_spec_code      VARCHAR,
    comm_div_rate       DECIMAL(18, 4),
    spec_div_rate       DECIMAL(18, 4),
    raw                 JSON
);

-- 取引カレンダー
CREATE TABLE IF NOT EXISTS trading_calendar (
    date             DATE PRIMARY KEY,
    holiday_division VARCHAR
);

-- 取得ログ
CREATE SEQUENCE IF NOT EXISTS fetch_log_seq;
CREATE TABLE IF NOT EXISTS fetch_log (
    id          BIGINT PRIMARY KEY DEFAULT nextval('fetch_log_seq'),
    job         VARCHAR NOT NULL,
    started_at  TIMESTAMP NOT NULL,  -- JST
    finished_at TIMESTAMP,           -- JST
    status      VARCHAR NOT NULL,
    rows        BIGINT,
    requests    BIGINT,
    params      JSON,
    error       VARCHAR
);
"""

# --- V2 → スキーマの対応表 -------------------------------------------------
# (保存先カラム, V2 フィールド, 型)

LISTED_INFO_SPEC: list[tuple[str, str, Kind]] = [
    ("code", "Code", "code"),
    ("name", "CoName", "str"),
    ("name_en", "CoNameEn", "str"),
    ("sector17", "S17", "str"),
    ("sector17_name", "S17Nm", "str"),
    ("sector33", "S33", "str"),
    ("sector33_name", "S33Nm", "str"),
    ("scale_cat", "ScaleCat", "str"),
    ("market", "Mkt", "str"),
    ("market_name", "MktNm", "str"),
    ("margin", "Mrgn", "str"),
    ("margin_name", "MrgnNm", "str"),
]

DAILY_BARS_SPEC: list[tuple[str, str, Kind]] = [
    ("code", "Code", "code"),
    ("date", "Date", "date"),
    ("open", "O", "float"),
    ("high", "H", "float"),
    ("low", "L", "float"),
    ("close", "C", "float"),
    ("upper_limit", "UL", "str"),
    ("lower_limit", "LL", "str"),
    ("volume", "Vo", "float"),
    ("turnover", "Va", "float"),
    ("adj_factor", "AdjFactor", "float"),
    ("adj_open", "AdjO", "float"),
    ("adj_high", "AdjH", "float"),
    ("adj_low", "AdjL", "float"),
    ("adj_close", "AdjC", "float"),
    ("adj_volume", "AdjVo", "float"),
]

INDEX_BARS_SPEC: list[tuple[str, str, Kind]] = [
    ("date", "Date", "date"),
    ("open", "O", "float"),
    ("high", "H", "float"),
    ("low", "L", "float"),
    ("close", "C", "float"),
]

FIN_SUMMARY_SPEC: list[tuple[str, str, Kind]] = [
    ("disc_no", "DiscNo", "str"),
    ("code", "Code", "code"),
    ("disclosed_date", "DiscDate", "date"),
    ("disclosed_time", "DiscTime", "str"),
    ("doc_type", "DocType", "str"),
    ("period_type", "CurPerType", "str"),
    ("period_start", "CurPerSt", "date"),
    ("period_end", "CurPerEn", "date"),
    ("fy_start", "CurFYSt", "date"),
    ("fy_end", "CurFYEn", "date"),
    ("sales", "Sales", "int"),
    ("operating_profit", "OP", "int"),
    ("ordinary_profit", "OdP", "int"),
    ("net_profit", "NP", "int"),
    ("eps", "EPS", "dec"),
    ("diluted_eps", "DEPS", "dec"),
    ("total_assets", "TA", "int"),
    ("equity", "Eq", "int"),
    ("equity_ratio", "EqAR", "dec"),
    ("bps", "BPS", "dec"),
    ("cfo", "CFO", "int"),
    ("cfi", "CFI", "int"),
    ("cff", "CFF", "int"),
    ("cash_eq", "CashEq", "int"),
    ("div_annual", "DivAnn", "dec"),
    ("payout_ratio", "PayoutRatioAnn", "dec"),
    ("f_div_annual", "FDivAnn", "dec"),
    ("f_sales", "FSales", "int"),
    ("f_operating_profit", "FOP", "int"),
    ("f_ordinary_profit", "FOdP", "int"),
    ("f_net_profit", "FNP", "int"),
    ("f_eps", "FEPS", "dec"),
    ("shares_outstanding", "ShOutFY", "int"),
    ("treasury_shares", "TrShFY", "int"),
    ("avg_shares", "AvgSh", "int"),
]

DIVIDENDS_SPEC: list[tuple[str, str, Kind]] = [
    ("ref_no", "RefNo", "str"),
    ("code", "Code", "code"),
    ("pub_date", "PubDate", "date"),
    ("pub_time", "PubTime", "str"),
    ("status_code", "StatCode", "str"),
    ("board_date", "BoardDate", "date"),
    ("interim_final_code", "IFCode", "str"),
    ("forecast_result_code", "FRCode", "str"),
    ("interim_final_term", "IFTerm", "str"),
    ("div_rate", "DivRate", "dec"),
    ("record_date", "RecDate", "date"),
    ("ex_date", "ExDate", "date"),
    ("actual_record_date", "ActRecDate", "date"),
    ("pay_date", "PayDate", "date"),
    ("ca_ref_no", "CARefNo", "str"),
    ("comm_spec_code", "CommSpecCode", "str"),
    ("comm_div_rate", "CommDivRate", "dec"),
    ("spec_div_rate", "SpecDivRate", "dec"),
]

CALENDAR_SPEC: list[tuple[str, str, Kind]] = [
    ("date", "Date", "date"),
    ("holiday_division", "HolDiv", "str"),
]

#: 「値なし」とみなす文字列（J-Quants は未定・該当なしを空文字や "-" で返す）
_MISSING = {"", "-", "－", "nan", "NaN", "None", "null"}


# --- 変換（純粋関数） -------------------------------------------------------


def _is_missing(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and v != v:  # NaN
        return True
    if v is pd.NaT:
        return True
    return isinstance(v, str) and v.strip() in _MISSING


def _to_date(v: Any) -> date | None:
    if _is_missing(v):
        return None
    if isinstance(v, datetime | pd.Timestamp):
        return v.date()
    if isinstance(v, date):
        return v
    ts = pd.to_datetime(str(v).strip(), errors="coerce")
    return None if pd.isna(ts) else ts.date()


def _to_decimal(v: Any) -> Decimal | None:
    if _is_missing(v):
        return None
    try:
        return Decimal(str(v).strip().replace(",", ""))
    except InvalidOperation:
        return None


def _to_int(v: Any) -> int | None:
    d = _to_decimal(v)
    return None if d is None else int(d.to_integral_value())


def _to_float(v: Any) -> float | None:
    d = _to_decimal(v)
    return None if d is None else float(d)


def _to_str(v: Any) -> str | None:
    return None if _is_missing(v) else str(v).strip()


_CONVERTERS = {
    "str": _to_str,
    "code": lambda v: None if _is_missing(v) else normalize_code(v),
    "date": _to_date,
    "int": _to_int,
    "dec": _to_decimal,
    "float": _to_float,
}


def _convert(df: pd.DataFrame, spec: list[tuple[str, str, Kind]]) -> pd.DataFrame:
    """対応表に沿って列名と型を変換する。V2 側に無い列は None で埋める。"""
    out: dict[str, list[Any]] = {}
    for dst, src, kind in spec:
        conv = _CONVERTERS[kind]
        values = df[src].tolist() if src in df.columns else [None] * len(df)
        out[dst] = [conv(v) for v in values]
    return pd.DataFrame(out, columns=[dst for dst, _, _ in spec], dtype=object)


def _raw_json(df: pd.DataFrame) -> list[str]:
    def default(o: Any) -> str:
        return o.isoformat() if hasattr(o, "isoformat") else str(o)

    records = df.astype(object).where(df.notna(), None).to_dict(orient="records")
    return [json.dumps(r, ensure_ascii=False, default=default) for r in records]


def to_listed_info(df: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """上場銘柄一覧を listed_info の形にする。"""
    out = _convert(df, LISTED_INFO_SPEC)
    out.insert(0, "as_of", as_of)
    return out


def to_daily_bars(df: pd.DataFrame) -> pd.DataFrame:
    """株価四本値を daily_bars の形にする。"""
    return _convert(df, DAILY_BARS_SPEC)


def to_index_bars(df: pd.DataFrame, index_code: str) -> pd.DataFrame:
    """指数四本値を index_bars の形にする（TOPIX API は Code を返さないので明示する）。"""
    out = _convert(df, INDEX_BARS_SPEC)
    out.insert(0, "index_code", index_code)
    return out


def to_fin_summary(df: pd.DataFrame) -> pd.DataFrame:
    """決算短信サマリを fin_summary の形にする。"""
    out = _convert(df, FIN_SUMMARY_SPEC)
    out["raw"] = _raw_json(df)
    return out


def to_dividends(df: pd.DataFrame) -> pd.DataFrame:
    """配当金情報を dividends の形にする。"""
    out = _convert(df, DIVIDENDS_SPEC)
    out["raw"] = _raw_json(df)
    return out


def to_trading_calendar(df: pd.DataFrame) -> pd.DataFrame:
    """取引カレンダーを trading_calendar の形にする。"""
    return _convert(df, CALENDAR_SPEC)


def _now_jst() -> datetime:
    """JST の現在時刻（タイムゾーン情報なし。DB には JST として保存する）。"""
    return datetime.now(JST).replace(tzinfo=None)


# --- DB --------------------------------------------------------------------


@dataclass
class FetchRecord:
    """`Store.fetch_job` の中で件数などを記録するための入れ物。"""

    rows: int = 0
    requests: int = 0
    params: dict[str, Any] = field(default_factory=dict)


class Store:
    """DuckDB の読み書き。`Store(":memory:")` でテスト用のメモリ DB になる。"""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path))
        self.con.execute("SET TimeZone = 'Asia/Tokyo'")
        self.init_schema()

    def close(self) -> None:
        """接続を閉じる。"""
        self.con.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def init_schema(self) -> None:
        """テーブルが無ければ作る（何度呼んでもよい）。"""
        self.con.execute(SCHEMA_SQL)
        self.con.execute(
            "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", [str(SCHEMA_VERSION)]
        )

    def _upsert(self, table: str, df: pd.DataFrame, key: list[str]) -> int:
        if df.empty:
            return 0
        missing_key = df[key].isna().any(axis=1)
        if missing_key.any():
            raise ValueError(f"{table}: 主キー {key} が欠けた行が {int(missing_key.sum())} 件")
        # 同じキーが複数あれば後勝ち（INSERT OR REPLACE は同一バッチ内の重複を許さない）
        df = df.drop_duplicates(subset=key, keep="last")
        cols = ", ".join(df.columns)
        self.con.register("_upsert_df", df)
        try:
            self.con.execute(f"INSERT OR REPLACE INTO {table} ({cols}) SELECT {cols} FROM _upsert_df")
        finally:
            self.con.unregister("_upsert_df")
        return len(df)

    # 書き込み（V2 の DataFrame をそのまま渡す）

    def upsert_listed_info(self, df: pd.DataFrame, as_of: date) -> int:
        """上場銘柄一覧を保存する。戻り値は保存件数。"""
        return self._upsert("listed_info", to_listed_info(df, as_of), ["as_of", "code"])

    def upsert_daily_bars(self, df: pd.DataFrame) -> int:
        """株価四本値を保存する。"""
        return self._upsert("daily_bars", to_daily_bars(df), ["code", "date"])

    def upsert_index_bars(self, df: pd.DataFrame, index_code: str = TOPIX_CODE) -> int:
        """指数四本値を保存する。"""
        return self._upsert("index_bars", to_index_bars(df, index_code), ["index_code", "date"])

    def upsert_fin_summary(self, df: pd.DataFrame) -> int:
        """決算短信サマリを保存する。"""
        return self._upsert("fin_summary", to_fin_summary(df), ["disc_no"])

    def upsert_dividends(self, df: pd.DataFrame) -> int:
        """配当金情報を保存する。"""
        return self._upsert("dividends", to_dividends(df), ["ref_no"])

    def upsert_trading_calendar(self, df: pd.DataFrame) -> int:
        """取引カレンダーを保存する。"""
        return self._upsert("trading_calendar", to_trading_calendar(df), ["date"])

    # 取得ログ

    @contextmanager
    def fetch_job(self, job: str, **params: Any) -> Iterator[FetchRecord]:
        """取得処理を囲み、開始・終了・件数・エラーを fetch_log に残す。

        例::

            with store.fetch_job("daily_bars", code="72030") as rec:
                rec.rows += store.upsert_daily_bars(df)
        """
        rec = FetchRecord(params=params)
        started = _now_jst()
        log_id = self.con.execute(
            "INSERT INTO fetch_log (job, started_at, status, params) VALUES (?, ?, 'running', ?)"
            " RETURNING id",
            [job, started, json.dumps(params, ensure_ascii=False, default=str)],
        ).fetchone()[0]
        try:
            yield rec
        except BaseException as e:
            self._finish_log(log_id, "error", rec, f"{type(e).__name__}: {e}")
            raise
        else:
            self._finish_log(log_id, "ok", rec, None)

    def _finish_log(self, log_id: int, status: str, rec: FetchRecord, error: str | None) -> None:
        self.con.execute(
            "UPDATE fetch_log SET finished_at = ?, status = ?, rows = ?, requests = ?, error = ?"
            " WHERE id = ?",
            [_now_jst(), status, rec.rows, rec.requests, error, log_id],
        )

    # 読み出し

    def last_date(self, table: str, code: str | None = None) -> date | None:
        """差分取得用：テーブルの最新日付（銘柄指定可）。データが無ければ None。"""
        date_col = {
            "daily_bars": "date",
            "index_bars": "date",
            "trading_calendar": "date",
            "fin_summary": "disclosed_date",
            "dividends": "pub_date",
            "listed_info": "as_of",
        }[table]
        sql = f"SELECT max({date_col}) FROM {table}"
        args: list[Any] = []
        if code is not None:
            key = "index_code" if table == "index_bars" else "code"
            sql += f" WHERE {key} = ?"
            args.append(code if table == "index_bars" else normalize_code(code))
        return self.con.execute(sql, args).fetchone()[0]

    def daily_bars(
        self, code: str, start: date | None = None, end: date | None = None
    ) -> pd.DataFrame:
        """1 銘柄の日足を日付順で返す。"""
        sql = "SELECT * FROM daily_bars WHERE code = ?"
        args: list[Any] = [normalize_code(code)]
        if start is not None:
            sql += " AND date >= ?"
            args.append(start)
        if end is not None:
            sql += " AND date <= ?"
            args.append(end)
        return self.con.execute(sql + " ORDER BY date", args).df()

    def index_bars(
        self, index_code: str = TOPIX_CODE, start: date | None = None, end: date | None = None
    ) -> pd.DataFrame:
        """指数の日足を日付順で返す。"""
        sql = "SELECT * FROM index_bars WHERE index_code = ?"
        args: list[Any] = [index_code]
        if start is not None:
            sql += " AND date >= ?"
            args.append(start)
        if end is not None:
            sql += " AND date <= ?"
            args.append(end)
        return self.con.execute(sql + " ORDER BY date", args).df()

    def dividends(self, code: str) -> pd.DataFrame:
        """1 銘柄の配当金情報を権利落ち日順で返す。"""
        return self.con.execute(
            "SELECT * EXCLUDE (raw) FROM dividends WHERE code = ? ORDER BY ex_date, pub_date",
            [normalize_code(code)],
        ).df()

    def fin_summary_known_at(self, code: str, as_of: date) -> pd.DataFrame:
        """`as_of` 時点で開示済みの決算短信だけを返す（ルックアヘッド防止）。"""
        return self.con.execute(
            "SELECT * EXCLUDE (raw) FROM fin_summary WHERE code = ? AND disclosed_date <= ?"
            " ORDER BY disclosed_date, disclosed_time",
            [normalize_code(code), as_of],
        ).df()

    def latest_listed_info(self) -> pd.DataFrame:
        """銘柄ごとに最も新しいスナップショットの行を返す（上場廃止銘柄も含む）。"""
        return self.con.execute(
            "SELECT * FROM listed_info QUALIFY row_number() OVER"
            " (PARTITION BY code ORDER BY as_of DESC) = 1 ORDER BY code"
        ).df()
