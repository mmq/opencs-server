"""The judge's ``--checks`` syntax (tools/judge.py)."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "judge", Path(__file__).parents[2] / "tools" / "judge.py"
)
assert SPEC is not None and SPEC.loader is not None
judge = importlib.util.module_from_spec(SPEC)
sys.modules["judge"] = judge  # dataclasses look their module up here
SPEC.loader.exec_module(judge)


def test_lists_ranges_and_mixtures() -> None:
    assert judge.parse_checks("1,2,5") == [1, 2, 5]
    assert judge.parse_checks("1-3") == [1, 2, 3]
    assert judge.parse_checks("1-14,17-19") == [*range(1, 15), 17, 18, 19]
    assert judge.parse_checks("5,3-4,4") == [3, 4, 5]
    assert judge.parse_checks("19-19") == [19]


@pytest.mark.parametrize("text", ["", "0", "20", "1-20", "0-3", "5-3", "a", "1-", "-3", "1--3"])
def test_bad_checks_are_refused(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        judge.parse_checks(text)


def test_random_pilots_cannot_collide_with_real_accounts() -> None:
    first, second = judge.Pilots.random(), judge.Pilots.random()
    assert first != second
    for pilots in (first, second):
        assert pilots.alpha.startswith("ALPHA") and pilots.bravo.startswith("BRAVO")
        assert pilots.alpha != "ALPHA" and pilots.bravo != "BRAVO"
        assert len(pilots.alpha) == len(pilots.bravo) == 8  # the fake client keeps 8 characters
        assert len(pilots.password) >= 16 and pilots.password != judge.DEFAULT_PASSWORD


def test_fixed_pilots_are_alpha_and_bravo() -> None:
    pilots = judge.Pilots()
    assert (pilots.alpha, pilots.bravo, pilots.password) == ("ALPHA", "BRAVO", "FAKECLIENT")


def test_random_names_are_mapped_back_for_the_checks() -> None:
    pilots = judge.Pilots.random()
    line = f"INTRODUCED to player '{pilots.bravo}' (from {pilots.alpha})"
    assert pilots.normalise(line) == "INTRODUCED to player 'BRAVO' (from ALPHA)"
    check = judge.CHECKS[3]  # 4, "each player introduced to the other"
    assert check.passed(
        pilots.normalise(f"INTRODUCED to player '{pilots.bravo}'"),
        pilots.normalise(f"INTRODUCED to player '{pilots.alpha}'"),
    )


def test_the_fake_client_takes_a_password() -> None:
    fake_spec = importlib.util.spec_from_file_location(
        "fake_client", Path(__file__).parents[2] / "tools" / "fake_client.py"
    )
    assert fake_spec is not None and fake_spec.loader is not None
    fake = importlib.util.module_from_spec(fake_spec)
    sys.modules["fake_client"] = fake
    fake_spec.loader.exec_module(fake)
    assert fake.parse_args([]).password == "FAKECLIENT"
    assert fake.parse_args(["--password", "s3cret"]).password == "s3cret"
