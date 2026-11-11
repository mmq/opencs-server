"""Password checks and new accounts are metered across connections (DESIGN §5)."""

from __future__ import annotations

from fixtures.sessions import DeferredHasher, World, connect, make_world
from opencs import policy
from opencs.clock import ManualClock
from opencs.lobby.identity import Pilot
from opencs.ratelimit import KeyedBuckets
from opencs.session.loginlimits import LoginLimits


def attempt(world: World, line: str, address: str) -> bytes:
    """A fresh connection from ``address`` sends one account line; what the server answered."""
    h = connect(world, address=address)
    h.feed(f"HHH\r{line}\r")
    out = h.take()
    h.session.closed()  # the player hangs up: the account is free again
    return out


def register(world: World, *names: str) -> None:
    """Accounts that exist already, made without the gate."""
    for name in names:
        pilot = world.identity.login(f"{name},sesame")
        assert isinstance(pilot, Pilot)
        world.identity.release(pilot)


def refused(out: bytes, fragment: bytes) -> bool:
    return fragment in out


def told_wrong(out: bytes) -> bool:
    """The password was checked and was wrong (not refused unchecked)."""
    return b"WRONG PASSWORD" in out and b"TOO MANY" not in out


def test_one_address_is_limited_in_password_checks_across_connections() -> None:
    world = make_world()
    burst = policy.LOGIN_ATTEMPTS_PER_ADDRESS[0]
    register(world, *(f"ACCT{n}" for n in range(burst + 1)))  # one guess each: no account limit
    outs = [attempt(world, f"ACCT{n},wrong", "1.1.1.1") for n in range(burst + 1)]
    assert not any(refused(o, b"TOO MANY") for o in outs[:burst])
    assert refused(outs[burst], b"TOO MANY LOGIN ATTEMPTS")


def test_the_allowance_comes_back_with_time() -> None:
    world = make_world()
    burst, rate = policy.LOGIN_ATTEMPTS_PER_ADDRESS
    register(world, "LAST", *(f"ACCT{n}" for n in range(burst)))
    for n in range(burst):
        attempt(world, f"ACCT{n},wrong", "1.1.1.1")
    assert refused(attempt(world, "LAST,x", "1.1.1.1"), b"TOO MANY")
    world.clock.advance(1 / rate - 1)
    assert refused(attempt(world, "LAST,x", "1.1.1.1"), b"TOO MANY")
    world.clock.advance(1)
    assert not refused(attempt(world, "LAST,x", "1.1.1.1"), b"TOO MANY")


def test_another_address_is_not_affected() -> None:
    world = make_world()
    register(world, "ACCT", "OTHER")
    for n in range(policy.LOGIN_ATTEMPTS_PER_ADDRESS[0] + 1):
        attempt(world, f"ACCT,wrong{n}", "1.1.1.1")
    assert not refused(attempt(world, "OTHER,sesame", "2.2.2.2"), b"TOO MANY")


def test_addresses_in_one_ipv6_slash_64_share_one_allowance_of_password_checks() -> None:
    world = make_world()
    burst = policy.LOGIN_ATTEMPTS_PER_ADDRESS[0]
    register(world, *(f"ACCT{n}" for n in range(burst + 1)))
    outs = [attempt(world, f"ACCT{n},wrong", f"2001:db8:0:1::{n + 1:x}") for n in range(burst + 1)]
    assert not any(refused(o, b"TOO MANY") for o in outs[:burst])
    assert refused(outs[burst], b"TOO MANY LOGIN ATTEMPTS")
    assert not refused(attempt(world, "ACCT0,sesame", "2001:db8:0:2::1"), b"TOO MANY")


def test_guests_cost_nothing() -> None:
    world = make_world()
    for _ in range(policy.LOGIN_ATTEMPTS_PER_ADDRESS[0] * 3):
        assert refused(attempt(world, "JOINGENIE", "1.1.1.1"), b"PASSWORD: ")


def test_new_accounts_are_limited_per_address() -> None:
    world = make_world()
    burst = policy.NEW_ACCOUNTS_PER_ADDRESS[0]
    outs = [attempt(world, f"NEW{n},sesame", "1.1.1.1") for n in range(burst + 1)]
    assert not any(refused(o, b"TOO MANY") for o in outs[:burst])
    assert refused(outs[burst], b"TOO MANY NEW ACCOUNTS")
    # An existing account still logs in from that address: only registration is refused.
    assert refused(attempt(world, "NEW0,sesame", "1.1.1.1"), b"PASSWORD: ")


def test_new_accounts_are_limited_for_the_whole_server() -> None:
    world = make_world()
    burst = policy.NEW_ACCOUNTS_GLOBAL[0]
    outs = [attempt(world, f"NEW{n},sesame", f"10.0.0.{n}") for n in range(burst + 1)]
    assert not any(refused(o, b"TOO MANY") for o in outs[:burst])
    assert refused(outs[burst], b"TOO MANY NEW ACCOUNTS")


def test_password_checks_are_limited_for_the_whole_server() -> None:
    world = make_world()
    burst = policy.LOGIN_ATTEMPTS_GLOBAL[0]
    register(world, *(f"ACCT{n}" for n in range(burst + 1)))
    outs = [attempt(world, f"ACCT{n},wrong", f"10.0.{n}.1") for n in range(burst + 1)]
    assert not any(refused(o, b"TOO MANY") for o in outs[:burst])
    assert refused(outs[burst], b"TOO MANY LOGIN ATTEMPTS")


def test_a_refused_address_does_not_drain_the_global_allowance() -> None:
    clock = ManualClock(0.0)
    limits = LoginLimits(clock)
    for _ in range(policy.LOGIN_ATTEMPTS_PER_ADDRESS[0]):
        assert limits.attempt("1.1.1.1")
    for _ in range(100):
        assert not limits.attempt("1.1.1.1")
    # The global bucket was charged only for the allowed ones.
    spent = policy.LOGIN_ATTEMPTS_PER_ADDRESS[0]
    for n in range(policy.LOGIN_ATTEMPTS_GLOBAL[0] - spent):
        assert limits.attempt(f"9.9.9.{n}")


def test_the_table_of_sources_is_bounded() -> None:
    buckets = KeyedBuckets(1, 1.0)
    for n in range(policy.MAX_TRACKED_SOURCES * 2):
        buckets.take(f"a{n}", 0.0)  # each spends its only token: none is full, none can be pruned
    assert len(buckets) <= policy.MAX_TRACKED_SOURCES


def test_refilled_sources_are_forgotten_first() -> None:
    buckets = KeyedBuckets(1, 1.0)
    for n in range(policy.MAX_TRACKED_SOURCES):
        buckets.take(f"a{n}", 0.0)
    buckets.take("fresh", 10.0)  # the table was full, every old bucket had refilled
    assert len(buckets) == 1


# --- one account, many addresses -------------------------------------------------------------


def test_wrong_passwords_for_one_account_are_limited_whatever_the_address() -> None:
    world = make_world()
    register(world, "VICTIM", "OTHER")
    burst = policy.FAILED_LOGINS_PER_ACCOUNT[0]
    outs = [attempt(world, "VICTIM,guess", f"10.1.{n}.1") for n in range(burst + 1)]
    assert all(told_wrong(o) for o in outs[:burst])
    assert refused(outs[burst], b"TOO MANY WRONG PASSWORDS FOR THAT ACCOUNT")
    assert refused(attempt(world, "VICTIM,sesame", "10.2.0.1"), b"TOO MANY WRONG")  # even the owner
    assert refused(attempt(world, "OTHER,sesame", "10.2.0.2"), b"PASSWORD: ")  # others unaffected


def test_a_locked_account_opens_again_in_minutes() -> None:
    world = make_world()
    register(world, "VICTIM")
    burst, rate = policy.FAILED_LOGINS_PER_ACCOUNT
    for n in range(burst):
        attempt(world, "VICTIM,guess", f"10.1.{n}.1")
    world.clock.advance(1 / rate)
    assert refused(attempt(world, "VICTIM,sesame", "10.2.0.1"), b"PASSWORD: ")


def test_a_guesser_cannot_lock_the_owner_out_where_the_owner_has_logged_in() -> None:
    world = make_world()
    register(world, "VICTIM")
    assert refused(attempt(world, "VICTIM,sesame", "10.9.0.1"), b"PASSWORD: ")
    burst = policy.FAILED_LOGINS_PER_ACCOUNT[0]
    outs = [attempt(world, "VICTIM,guess", f"10.1.{n}.1") for n in range(burst * 2)]
    assert refused(outs[-1], b"TOO MANY WRONG PASSWORDS")  # the guessers are stopped
    assert refused(attempt(world, "VICTIM,guess", "10.1.0.1"), b"TOO MANY WRONG PASSWORDS")
    assert refused(attempt(world, "VICTIM,sesame", "10.9.0.1"), b"PASSWORD: ")  # the owner is not


def test_the_owner_s_known_source_is_its_ipv6_slash_64_not_one_address() -> None:
    world = make_world()
    register(world, "VICTIM")
    assert refused(attempt(world, "VICTIM,sesame", "2001:db8:9::1"), b"PASSWORD: ")
    burst = policy.FAILED_LOGINS_PER_ACCOUNT[0]
    for n in range(burst * 2):
        attempt(world, "VICTIM,guess", f"10.1.{n}.1")
    # The owner's host picked a new address in the same /64: still the source it logged in from.
    assert refused(attempt(world, "VICTIM,sesame", "2001:db8:9::2"), b"PASSWORD: ")


def test_wrong_passwords_from_a_source_the_owner_uses_are_still_limited() -> None:
    world = make_world()
    register(world, "VICTIM")
    attempt(world, "VICTIM,sesame", "10.9.0.1")
    burst = policy.FAILED_LOGINS_PER_ACCOUNT_AND_SOURCE[0]
    outs = [attempt(world, "VICTIM,guess", "10.9.0.1") for _ in range(burst + 1)]
    assert all(told_wrong(o) for o in outs[:burst])
    assert refused(outs[burst], b"TOO MANY WRONG PASSWORDS")


def test_one_source_s_wrong_passwords_spend_nothing_of_another_known_source() -> None:
    world = make_world()
    register(world, "VICTIM")
    attempt(world, "VICTIM,sesame", "10.8.0.1")  # a housemate, say
    attempt(world, "VICTIM,sesame", "10.9.0.1")
    for _ in range(policy.FAILED_LOGINS_PER_ACCOUNT_AND_SOURCE[0]):
        attempt(world, "VICTIM,guess", "10.8.0.1")
    assert refused(attempt(world, "VICTIM,sesame", "10.9.0.1"), b"PASSWORD: ")


def test_an_account_remembers_only_its_latest_sources() -> None:
    clock = ManualClock(0.0)
    limits = LoginLimits(clock)
    sources = [f"10.9.0.{n}" for n in range(policy.KNOWN_SOURCES_PER_ACCOUNT + 1)]
    for source in sources:
        limits.account_succeeded("VICTIM", source)
    for n in range(policy.FAILED_LOGINS_PER_ACCOUNT[0]):
        limits.try_account("VICTIM", f"10.1.{n}.1")
    # The oldest was forgotten, so it is held to the guessers' limit; the others are not.
    assert limits.try_account("VICTIM", sources[0]) is None
    assert all(limits.try_account("VICTIM", s) is not None for s in sources[1:])


def test_the_table_of_known_sources_is_bounded() -> None:
    limits = LoginLimits(ManualClock(0.0))
    for n in range(policy.MAX_TRACKED_SOURCES * 2):
        limits.account_succeeded(f"ACCT{n}", "10.9.0.1")
    assert len(limits._known) <= policy.MAX_TRACKED_SOURCES


def test_forgetting_an_account_forgets_its_failures_and_sources() -> None:
    limits = LoginLimits(ManualClock(0.0))
    limits.account_succeeded("VICTIM", "10.9.0.1")
    for _ in range(policy.FAILED_LOGINS_PER_ACCOUNT_AND_SOURCE[0]):
        limits.try_account("VICTIM", "10.9.0.1")
    for n in range(policy.FAILED_LOGINS_PER_ACCOUNT[0]):
        limits.try_account("VICTIM", f"10.1.{n}.1")
    limits.forget_account("VICTIM")
    assert limits.try_account("VICTIM", "10.9.0.1") is not None
    assert limits.try_account("VICTIM", "10.2.0.1") is not None


def test_a_refused_check_spends_nothing_from_the_account() -> None:
    world = make_world()
    register(world, "VICTIM")
    for n in range(policy.FAILED_LOGINS_PER_ACCOUNT[0] * 3):
        attempt(world, "VICTIM,guess", f"10.1.{n}.1")
    world.clock.advance(1 / policy.FAILED_LOGINS_PER_ACCOUNT[1])  # one failure's worth
    assert refused(attempt(world, "VICTIM,sesame", "10.2.0.1"), b"PASSWORD: ")


# --- guesses at once ---------------------------------------------------------------------------


def test_guesses_made_at_once_are_charged_before_any_is_hashed() -> None:
    hasher = DeferredHasher()
    world = make_world(hasher=hasher)
    register(world, "VICTIM")
    guessers = [connect(world) for _ in range(policy.LOGIN_ATTEMPTS_GLOBAL[0])]
    for h in guessers:
        h.feed("HHH\rVICTIM,guess\r")
    burst = policy.FAILED_LOGINS_PER_ACCOUNT[0]
    assert len(hasher.waiting) == burst
    hasher.run()
    outs = [h.take() for h in guessers]
    locked = sum(refused(o, b"TOO MANY WRONG PASSWORDS") for o in outs)
    assert locked == len(guessers) - burst  # the rest were each told the password was wrong


def test_a_right_password_gives_its_charge_back() -> None:
    world = make_world()
    register(world, "VICTIM")
    burst = policy.FAILED_LOGINS_PER_ACCOUNT[0]
    for n in range(burst - 1):
        attempt(world, "VICTIM,guess", f"10.1.{n}.1")
    assert refused(attempt(world, "VICTIM,sesame", "10.2.0.1"), b"PASSWORD: ")
    assert told_wrong(attempt(world, "VICTIM,guess", "10.3.0.1"))  # the last one
    assert refused(attempt(world, "VICTIM,guess", "10.3.1.1"), b"TOO MANY WRONG PASSWORDS")


def test_a_check_the_address_may_not_make_charges_nothing_to_the_account() -> None:
    world = make_world()
    burst = policy.LOGIN_ATTEMPTS_PER_ADDRESS[0]
    register(world, "VICTIM", *(f"ACCT{n}" for n in range(burst)))
    for n in range(burst):
        attempt(world, f"ACCT{n},wrong", "1.1.1.1")
    for _ in range(policy.FAILED_LOGINS_PER_ACCOUNT[0] * 2):
        assert refused(attempt(world, "VICTIM,guess", "1.1.1.1"), b"TOO MANY LOGIN ATTEMPTS")
    guesses = policy.FAILED_LOGINS_PER_ACCOUNT[0]
    outs = [attempt(world, "VICTIM,guess", f"10.1.{n}.1") for n in range(guesses)]
    assert all(told_wrong(o) for o in outs)


def test_a_guess_whose_player_hung_up_during_the_check_stays_charged() -> None:
    hasher = DeferredHasher()
    world = make_world(hasher=hasher)
    register(world, "VICTIM")
    for n in range(policy.FAILED_LOGINS_PER_ACCOUNT[0]):
        h = connect(world, address=f"10.1.{n}.1")
        h.feed("HHH\rVICTIM,guess\r")
        h.session.closed()
    hasher.run()
    assert refused(attempt(world, "VICTIM,guess", "10.3.0.1"), b"TOO MANY WRONG PASSWORDS")
