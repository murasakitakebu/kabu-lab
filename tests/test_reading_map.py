"""core/reports/reading_map のユニットテスト。"""

import pytest

from core.reports.reading_map import (
    STRATEGY_GUIDES,
    get_all_reading_points,
    get_common_red_flags,
    get_guide,
)

STRATEGY_KEYS = list(STRATEGY_GUIDES.keys())


@pytest.mark.parametrize("key", STRATEGY_KEYS)
def test_get_guide_returns_guide(key: str) -> None:
    guide = get_guide(key)
    assert guide is not None
    assert guide.strategy_key == key
    assert guide.strategy_name


@pytest.mark.parametrize("key", STRATEGY_KEYS)
def test_reading_points_non_empty(key: str) -> None:
    points = get_all_reading_points(key)
    assert len(points) > 0


@pytest.mark.parametrize("key", STRATEGY_KEYS)
def test_reading_points_no_duplicates(key: str) -> None:
    points = get_all_reading_points(key)
    sections = [p.section for p in points]
    assert len(sections) == len(set(sections)), "重複する section が存在する"


@pytest.mark.parametrize("key", STRATEGY_KEYS)
def test_reading_points_sorted_by_priority(key: str) -> None:
    points = get_all_reading_points(key)
    priorities = [p.priority for p in points]
    assert priorities == sorted(priorities), "優先度順になっていない"


def test_common_red_flags_non_empty() -> None:
    flags = get_common_red_flags()
    assert len(flags) > 0
    for f in flags:
        assert f.label
        assert f.description


def test_get_guide_unknown_key_returns_none() -> None:
    assert get_guide("nonexistent") is None
