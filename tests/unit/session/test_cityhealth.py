"""What is kept about each city's failures and slow calls (DESIGN §5, §6)."""

from __future__ import annotations

import logging

import pytest

from opencs import policy
from opencs.session.cityhealth import CityHealth


def warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_a_call_under_the_limit_is_not_noted_and_one_at_it_is() -> None:
    health = CityHealth()
    health.called("A", "a tick", 100.0, 100.0 + policy.SLOW_CITY_CALL_SECONDS / 2)
    assert health.slow_calls("A") == 0
    health.called("A", "a tick", 100.0, 100.0 + policy.SLOW_CITY_CALL_SECONDS * 2)
    assert health.slow_calls("A") == 1
    assert health.worst_call("A") == pytest.approx(policy.SLOW_CITY_CALL_SECONDS * 2)


def test_warnings_are_limited_per_city_and_say_how_many_went_unlogged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="opencs.session")
    health = CityHealth()
    health.called("A", "a tick", 100.0, 100.05)
    health.called("A", "Shot", 110.0, 110.03)
    health.called("A", "a tick", 120.0, 120.03)
    health.called("B", "a tick", 120.0, 120.04)  # another city has its own allowance
    assert warnings(caplog) == [
        "city A: a tick took 50 ms; 1 slow since the last warning",
        "city B: a tick took 40 ms; 1 slow since the last warning",
    ]
    health.called("A", "PodState", 200.0, 200.06)
    assert warnings(caplog)[-1] == "city A: PodState took 60 ms; 3 slow since the last warning"
    assert health.slow_calls("A") == 4


def test_the_record_outlasts_a_rebuild_but_not_the_city() -> None:
    health = CityHealth()
    health.failed("A")
    health.called("A", "a tick", 100.0, 100.05)
    health.replaced("A")
    assert not health.tainted("A")
    assert (health.failures("A"), health.rebuilds("A"), health.slow_calls("A")) == (1, 1, 1)
    health.replaced("A")  # a reload of a city that has not failed since is not a rebuild for this
    assert health.rebuilds("A") == 1
    health.forget("A")
    assert (health.failures("A"), health.rebuilds("A"), health.slow_calls("A")) == (0, 0, 0)
    assert health.worst_call("A") == 0.0
