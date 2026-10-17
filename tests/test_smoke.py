"""雛形が import できることの確認。"""

import importlib

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "core",
        "core.data",
        "core.calc",
        "core.strategies",
        "core.backtest",
        "core.paper",
        "core.reports",
    ],
)
def test_core_packages_importable(module: str) -> None:
    importlib.import_module(module)
