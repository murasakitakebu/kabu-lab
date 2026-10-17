"""core.data.jquants のテスト（ネットワークには出ない）。"""

from __future__ import annotations

import itertools
import threading
from datetime import date
from typing import Any

import pandas as pd
import pytest
import requests
from jquantsapi import ClientV2
from requests.exceptions import HTTPError

from core.data.jquants import (
    RATE_LIMITS,
    JQuantsClient,
    Plan,
    RateLimiter,
    RetryPolicy,
    ThrottledClientV2,
    display_code,
    normalize_code,
)


class FakeClock:
    """sleep すると時間が進む擬似時計。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self.now += seconds


def _response(status: int, headers: dict[str, str] | None = None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp.headers.update(headers or {})
    resp.url = "https://api.jquants.com/v2/test"
    return resp


def _http_error(status: int, headers: dict[str, str] | None = None) -> HTTPError:
    return HTTPError(f"{status}", response=_response(status, headers))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _client(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, outcomes: list[Any], rpm: float = 60
) -> ThrottledClientV2:
    """ClientV2._get が outcomes を順に返す（例外なら送出する）クライアント。"""
    calls = iter(outcomes)

    def fake_get(self: ClientV2, url: str, params: dict[str, Any] | None = None) -> Any:
        out = next(calls)
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(ClientV2, "_get", fake_get)
    limiter = RateLimiter(rpm, safety=1.0, clock=clock, sleep=clock.sleep)
    return ThrottledClientV2(
        "dummy-key",
        rate_limiter=limiter,
        retry=RetryPolicy(max_attempts=4, base_delay=2.0, max_delay=30.0),
        sleep=clock.sleep,
    )


# --- RateLimiter ---------------------------------------------------------


def test_rate_limiter_spaces_requests(clock: FakeClock) -> None:
    limiter = RateLimiter(5, safety=1.0, clock=clock, sleep=clock.sleep)
    for _ in range(4):
        limiter.acquire()
    # 1 回目は即時、以降 12 秒間隔
    assert clock.sleeps == [12.0, 12.0, 12.0]


def test_rate_limiter_no_wait_after_idle(clock: FakeClock) -> None:
    limiter = RateLimiter(5, safety=1.0, clock=clock, sleep=clock.sleep)
    limiter.acquire()
    clock.now += 100
    limiter.acquire()
    assert clock.sleeps == []


def test_rate_limiter_safety_margin() -> None:
    assert RateLimiter(60, safety=0.5).interval == pytest.approx(2.0)


def test_rate_limiter_penalize_delays_next(clock: FakeClock) -> None:
    limiter = RateLimiter(60, safety=1.0, clock=clock, sleep=clock.sleep)
    limiter.acquire()
    limiter.penalize(30)
    limiter.acquire()
    assert clock.sleeps == [30.0]


def test_rate_limiter_thread_safe_under_limit() -> None:
    """複数スレッドから叩いても 1 分あたりの上限を超えない（実時間・短間隔で確認）。"""
    import time

    limiter = RateLimiter(600, safety=1.0)  # 0.1 秒間隔
    stamps: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(3):
            limiter.acquire()
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    assert len(stamps) == 12
    assert min(gaps) >= 0.09  # sleep の誤差を少し許容


@pytest.mark.parametrize("plan", list(Plan))
def test_all_plans_have_limits(plan: Plan) -> None:
    assert RATE_LIMITS[plan] > 0


# --- RetryPolicy ---------------------------------------------------------


def test_retry_delay_exponential_and_capped() -> None:
    p = RetryPolicy(base_delay=2, max_delay=10)
    assert [p.delay(n) for n in range(1, 5)] == [2, 4, 8, 10]


def test_retry_delay_honors_retry_after() -> None:
    p = RetryPolicy(base_delay=2, max_delay=60)
    assert p.delay(1, retry_after=25) == 25
    assert p.delay(3, retry_after=1) == 8  # バックオフの方が長ければそちら
    assert p.delay(1, retry_after=999) == 60  # 上限で頭打ち


# --- ThrottledClientV2 ---------------------------------------------------


def test_get_success_first_try(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    ok = _response(200)
    c = _client(clock, monkeypatch, [ok])
    assert c._get("u") is ok
    assert c.request_count == 1
    assert clock.sleeps == []


def test_get_retries_on_429_with_retry_after(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = _response(200)
    c = _client(clock, monkeypatch, [_http_error(429, {"Retry-After": "20"}), ok])
    assert c._get("u") is ok
    assert c.request_count == 2
    assert sum(clock.sleeps) >= 20


def test_get_retries_on_5xx_and_connection_error(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = _response(200)
    c = _client(
        clock,
        monkeypatch,
        [_http_error(503), requests.ConnectionError("boom"), ok],
        rpm=6000,  # スロットリング待ちを無視できる程度に
    )
    assert c._get("u") is ok
    assert c.request_count == 3
    assert clock.sleeps[:2] == [2.0, 4.0]


def test_get_gives_up_after_max_attempts(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(clock, monkeypatch, [_http_error(500)] * 4)
    with pytest.raises(HTTPError):
        c._get("u")
    assert c.request_count == 4


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_get_does_not_retry_client_errors(
    status: int, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _client(clock, monkeypatch, [_http_error(status)])
    with pytest.raises(HTTPError):
        c._get("u")
    assert c.request_count == 1


def test_urllib3_retry_disabled(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(clock, monkeypatch, [])
    adapter = c._request_session().get_adapter("https://api.jquants.com")
    assert adapter.max_retries.total == 0


# --- コード正規化・ファサード ----------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7203", "72030"),
        (7203, "72030"),
        ("72030", "72030"),
        ("130a", "130A0"),
        (" 7974 ", "79740"),
    ],
)
def test_normalize_code(raw: str | int, expected: str) -> None:
    assert normalize_code(raw) == expected


@pytest.mark.parametrize("raw", ["", "720", "720300"])
def test_normalize_code_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_code(raw)


def test_display_code() -> None:
    assert display_code("72030") == "7203"
    assert display_code("7203") == "7203"
    assert display_code("25935") == "25935"


class _StubRaw:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def get_eq_bars_daily(self, **kwargs: Any) -> pd.DataFrame:
        self.kwargs = kwargs
        return pd.DataFrame({"Code": ["7203", "79740"], "Date": ["2024-01-04", "2024-01-04"]})


def test_facade_formats_dates_and_normalizes_codes() -> None:
    stub = _StubRaw()
    client = JQuantsClient(stub)  # type: ignore[arg-type]
    df = client.daily_bars("7203", start=date(2024, 1, 1), end=date(2024, 1, 31))
    assert stub.kwargs == {
        "code": "72030",
        "from_yyyymmdd": "2024-01-01",
        "to_yyyymmdd": "2024-01-31",
        "date_yyyymmdd": "",
    }
    assert df["Code"].tolist() == ["72030", "79740"]


def test_facade_requires_code_or_date() -> None:
    client = JQuantsClient(_StubRaw())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        client.daily_bars()


def test_from_env_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.data.jquants.load_dotenv", lambda: None)
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="JQUANTS_API_KEY"):
        JQuantsClient.from_env()


def test_from_env_uses_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.data.jquants.load_dotenv", lambda: None)
    monkeypatch.setenv("JQUANTS_API_KEY", "dummy")
    monkeypatch.setenv("JQUANTS_PLAN", "light")
    client = JQuantsClient.from_env()
    assert client.raw.rate_limiter.interval == pytest.approx(60 / (60 * 0.9))
