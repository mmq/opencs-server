"""Identity: real accounts, JOINGENIE guests, callsigns (II §3, II §3)."""

from __future__ import annotations

import threading
import time

import pytest

from opencs import policy
from opencs.lobby.identity import Identity, Pilot, Refused, hash_password, identity_key
from opencs.store.base import PBKDF2_SHA256, Account, Kdf
from opencs.store.memory import MemoryAccountStore

FAST = Kdf.argon2id(memory_kib=8, time_cost=1, parallelism=1, hash_bytes=32)  # cheap, same kind


def make(guests: tuple[str, ...] = ("JOINGENIE",)) -> tuple[Identity, MemoryAccountStore]:
    store = MemoryAccountStore()
    return Identity(store, guest_names=guests, kdf=FAST), store


def pilot(result: Pilot | Refused) -> Pilot:
    assert isinstance(result, Pilot), result
    return result


def test_joingenie_callsigns_are_the_name_then_numbered() -> None:
    identity, _ = make()
    names = [pilot(identity.login("JOINGENIE")).callsign for _ in range(3)]
    assert names == ["JOINGENIE", "JOINGENIE2", "JOINGENIE3"]


def test_joingenie_is_case_insensitive_and_has_no_password() -> None:
    identity, _ = make()
    first = pilot(identity.login("joingenie"))
    assert first == Pilot("JOINGENIE", None)
    assert first.is_guest
    # A password after JOINGENIE is not a registration of the guest name.
    assert pilot(identity.login("JoinGenie,sesame")).callsign == "JOINGENIE2"


def test_a_freed_callsign_is_reused() -> None:
    identity, _ = make()
    a = pilot(identity.login("JOINGENIE"))
    b = pilot(identity.login("JOINGENIE"))
    identity.release(a)
    assert pilot(identity.login("JOINGENIE")).callsign == "JOINGENIE"
    assert identity.callsign_online(b.callsign)


def test_later_duplicates_get_numbers_up_to_999_then_are_refused() -> None:
    identity, _ = make()
    calls = [pilot(identity.login("JOINGENIE")).callsign for _ in range(999)]
    assert calls[8:10] == ["JOINGENIE9", "JOINGENIE10"]
    assert calls[98:100] == ["JOINGENIE99", "JOINGENIE100"]
    assert calls[-1] == "JOINGENIE999"
    assert all(len(c) <= 12 for c in calls)
    assert isinstance(identity.login("JOINGENIE"), Refused)


def test_a_twelve_character_name_gives_up_its_tail_to_the_number() -> None:
    identity, _ = make(("ABCDEFGHIJKLM",))
    calls = [pilot(identity.login("ABCDEFGHIJKLM")).callsign for _ in range(101)]
    assert calls[:2] == ["ABCDEFGHIJKL", "ABCDEFGHIJK2"]
    assert calls[9] == "ABCDEFGHIJ10"
    assert calls[100] == "ABCDEFGHI101"


def test_short_names_get_the_digit_appended() -> None:
    identity, _ = make(("JOINGENIE", "BOB"))
    assert pilot(identity.login("BOB")).callsign == "BOB"
    assert pilot(identity.login("bob")).callsign == "BOB2"


def test_a_bare_name_that_is_not_a_guest_name_needs_a_password() -> None:
    identity, store = make()
    refused = identity.login("ALICE")
    assert isinstance(refused, Refused) and "PASSWORD" in refused.reason
    assert store.get("ALICE") is None


def test_configured_guest_names_ignore_a_password_and_may_be_shared() -> None:
    identity, store = make(("JOINGENIE", "visitor"))
    first, second = pilot(identity.login("VISITOR,one")), pilot(identity.login("Visitor,two"))
    assert (first, second) == (Pilot("VISITOR", None), Pilot("VISITOR2", None))
    assert store.get("VISITOR") is None


def test_with_no_guest_names_joingenie_is_refused_and_registers_nothing() -> None:
    identity, store = make(())
    assert isinstance(identity.login("JOINGENIE"), Refused)
    assert isinstance(identity.login("JOINGENIE,sesame"), Refused)
    assert store.get("JOINGENIE") is None
    assert pilot(identity.login("ALICE,sesame")).account == "ALICE"


def test_first_login_registers_the_password_and_later_ones_must_match() -> None:
    identity, store = make()
    first = pilot(identity.login("alice,tangerine2"))
    assert first == Pilot("ALICE", "ALICE")
    assert not first.is_guest
    assert store.get("ALICE") is not None
    identity.release(first)
    assert isinstance(identity.login("ALICE,wrong"), Refused)
    assert isinstance(identity.login("ALICE,tangerine"), Refused)
    again = identity.login("Alice,tangerine2")
    assert pilot(again).callsign == "ALICE"


def test_a_second_simultaneous_login_is_refused_and_a_release_allows_it() -> None:
    identity, _ = make()
    first = pilot(identity.login("ALICE,sesame"))
    refused = identity.login("ALICE,sesame")
    assert isinstance(refused, Refused)
    assert "ALREADY" in refused.reason
    identity.release(first)
    assert isinstance(identity.login("ALICE,sesame"), Pilot)


def test_the_password_may_hold_commas_and_must_not_be_empty_or_huge() -> None:
    identity, _ = make()
    assert isinstance(identity.login("ALICE,a,b,cdef"), Pilot)
    identity.release(Pilot("ALICE", "ALICE"))
    assert isinstance(identity.login("ALICE,a,b,cdef"), Pilot)
    assert isinstance(identity.login("BOB,"), Refused)
    assert isinstance(identity.login("CAROL," + "x" * 64), Pilot)
    assert isinstance(identity.login("DAN," + "x" * 65), Refused)


def test_name_bounds() -> None:
    identity, _ = make()
    assert isinstance(identity.login(""), Refused)
    assert isinstance(identity.login("  ,sesame"), Refused)
    assert isinstance(identity.login("N" * 33 + ",sesame"), Refused)
    assert isinstance(identity.login("N" * 13 + ",sesame"), Refused)
    assert pilot(identity.login("N" * 12 + ",sesame")).callsign == "N" * 12


def test_a_guest_cannot_take_a_registered_accounts_name() -> None:
    identity, store = make()
    identity.release(pilot(identity.login("ALICE,sesame")))
    guests = Identity(store, guest_names=("ALICE", "ZED"), kdf=FAST)
    refused = guests.login("alice")
    assert isinstance(refused, Refused)
    assert "REGISTERED" in refused.reason
    # An unregistered guest name is a guest named after itself.
    assert pilot(guests.login("zed")) == Pilot("ZED", None)


def test_a_guest_cannot_take_a_registered_accounts_callsign_by_truncation() -> None:
    # The account was registered before the guest name was configured (it would be refused now).
    store = MemoryAccountStore()
    no_guests = Identity(store, guest_names=(), kdf=FAST)
    no_guests.release(pilot(no_guests.login("ALPHABRAVOXY,sesame")))
    identity = Identity(store, guest_names=("ALPHABRAVOXYW",), kdf=FAST)
    refused = identity.login("ALPHABRAVOXYW")
    assert isinstance(refused, Refused) and "REGISTERED" in refused.reason
    assert pilot(identity.login("ALPHABRAVOXY,sesame")).callsign == "ALPHABRAVOXY"


def test_joingenie_with_a_password_is_a_guest_and_registers_nothing() -> None:
    identity, store = make()
    guest = pilot(identity.login("joingenie,x"))
    assert guest.is_guest
    assert store.get("JOINGENIE") is None


def test_a_guest_passes_over_a_registered_accounts_callsign() -> None:
    # JOINGENIE2 was registered while guests were off; turned back on, they must not take it.
    store = MemoryAccountStore()
    no_guests = Identity(store, guest_names=(), kdf=FAST)
    no_guests.release(pilot(no_guests.login("JOINGENIE2,sesame")))
    identity = Identity(store, guest_names=("JOINGENIE",), kdf=FAST)
    guests = [pilot(identity.login("JOINGENIE")).callsign for _ in range(2)]
    assert guests == ["JOINGENIE", "JOINGENIE3"]
    assert pilot(identity.login("JOINGENIE2,sesame")).callsign == "JOINGENIE2"


def test_the_stored_hash_is_salted_argon2id_not_the_password() -> None:
    identity, store = make()
    identity.login("ALICE,tangerine2")
    identity.login("BOB,tangerine2")
    alice, bob = store.get("ALICE"), store.get("BOB")
    assert alice is not None and bob is not None
    assert alice.password_hash != b"tangerine2"
    assert b"tangerine2" not in alice.password_hash
    assert alice.salt != bob.salt  # a fresh salt per account
    assert alice.password_hash != bob.password_hash  # so equal passwords hash differently
    assert alice.password_hash == hash_password("tangerine2", alice.salt, FAST)
    assert alice.kdf == FAST
    assert len(alice.password_hash) == 32


def test_new_hashes_are_made_with_the_policy_argon2id_cost() -> None:
    assert Identity(MemoryAccountStore())._kdf == Kdf.argon2id(
        memory_kib=policy.ARGON2_MEMORY_KIB,
        time_cost=policy.ARGON2_TIME_COST,
        parallelism=policy.ARGON2_PARALLELISM,
        hash_bytes=policy.ARGON2_HASH_BYTES,
    )


def test_hash_password_matches_the_argon2id_reference_vector() -> None:
    # The Argon2 reference implementation's test vector (argon2id, v=19, m=2^16, t=2, p=1,
    # P="password", S="somesalt").
    out = hash_password("password", b"somesalt", Kdf.argon2id(65536, 2, 1, 32))
    assert out.hex() == "09316115d5cf24ed5a15a31a3ba326e5cf32edc24702987c02b6566f61913cf7"


def test_an_unknown_algorithm_is_never_taken_for_another() -> None:
    with pytest.raises(ValueError, match="scrypt"):
        hash_password("pw", b"salt", Kdf("scrypt", 1))


def test_two_argon2id_hashes_run_side_by_side() -> None:
    # The hashing threads (app.ThreadHasher) work at once only if a hash releases the GIL. While
    # two run, this thread keeps running Python: its longest stall stays far below one hash.
    kdf = Kdf.argon2id(policy.ARGON2_MEMORY_KIB * 4, 4, 1, 32)  # long enough to measure
    began = time.perf_counter()
    hash_password("pw", b"0" * 16, kdf)
    one = time.perf_counter() - began
    workers = [
        threading.Thread(target=hash_password, args=("pw", bytes([n]) * 16, kdf)) for n in (1, 2)
    ]
    last = time.perf_counter()
    stall = 0.0
    for worker in workers:
        worker.start()
    while any(worker.is_alive() for worker in workers):
        now = time.perf_counter()
        stall, last = max(stall, now - last), now
    for worker in workers:
        worker.join()
    assert stall < one / 2


def test_hash_password_matches_the_pbkdf2_hmac_sha256_reference_vector() -> None:
    # RFC 7914 §11 test vector for PBKDF2-HMAC-SHA-256 (P="passwd", S="salt", c=1, dkLen=64).
    out = hash_password("passwd", b"salt", Kdf(PBKDF2_SHA256, 1, hash_bytes=64))
    assert out.hex().startswith("55ac046e56e3089fec1691c22544b605f94185216dde0465e68b9d57c20dacbc")


@pytest.mark.parametrize("line", ["ALICE,sesame", "JOINGENIE"])
def test_release_of_an_unknown_pilot_is_harmless(line: str) -> None:
    identity, _ = make()
    identity.release(Pilot("NOBODY", None))
    assert isinstance(identity.login(line), Pilot)


# --- progress keys (II §3) ---------------------------------------------------


def test_a_real_account_is_keyed_by_the_account_not_the_callsign() -> None:
    identity, _ = make()
    alice = pilot(identity.login("ALICE,sesame"))
    guest = pilot(identity.login("JOINGENIE"))
    assert (alice.callsign, alice.key) == ("ALICE", "acct:ALICE")
    assert (guest.callsign, guest.key) == ("JOINGENIE", "JOINGENIE")


def test_a_real_account_keeps_its_key_when_a_guest_holds_its_callsign() -> None:
    identity, _ = make()
    identity.release(pilot(identity.login("ZED,sesame")))  # registered
    guest = pilot(identity.login("JOINGENIE"))
    squatter = pilot(identity.login("ZED,sesame"))
    assert squatter.key == "acct:ZED" and guest.key == "JOINGENIE"
    identity.release(squatter)
    identity.release(guest)
    assert pilot(identity.login("ZED,sesame")).key == "acct:ZED"


def test_joingenie_is_keyed_by_the_assigned_callsign_in_connection_order() -> None:
    identity, _ = make()
    first, second, third = (pilot(identity.login("JOINGENIE")) for _ in range(3))
    assert [first.key, second.key, third.key] == ["JOINGENIE", "JOINGENIE2", "JOINGENIE3"]
    identity.release(second)
    assert pilot(identity.login("JOINGENIE")).key == "JOINGENIE2"  # a freed callsign is a free key


def test_a_guest_key_cannot_equal_an_account_key() -> None:
    identity, _ = make(("acct:zed",))
    guest = pilot(identity.login("acct:zed"))  # upper-cased, so never the lower-case prefix
    assert guest.key == "ACCT:ZED"
    assert identity_key("ZED", "ZED") == "acct:ZED" and identity_key("ZED", None) == "ZED"


# --- rules for new accounts ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "fragment"),
    [
        ("AB,sesame", "3 TO 12 CHARACTERS"),
        ("ALEXANDERTHE1,sesame", "3 TO 12 CHARACTERS"),
        ("AL ICE,sesame", "ONLY LETTERS"),
        ("ALICE!,sesame", "ONLY LETTERS"),
        ("ADMIN,sesame", "RESERVED"),
        ("SERVER,sesame", "RESERVED"),
        ("GUPPY,sesame", "RESERVED"),
        ("JOINGENIE2,sesame", "RESERVED"),
        ("JOINGENIE100,sesame", "RESERVED"),
        ("ALICE,", "PASSWORD REQUIRED"),
        ("ALICE,short", "6 CHARACTERS OR MORE"),
    ],
)
def test_a_new_account_must_have_a_plain_name_and_a_real_password(line: str, fragment: str) -> None:
    identity, store = make()
    refused = identity.login(line)
    assert isinstance(refused, Refused), refused
    assert fragment in refused.reason
    assert store.count() == 0


@pytest.mark.parametrize("password", ["123456", "password", "Qwerty", "LETMEIN", "p@ssw0rd"])
def test_a_common_password_is_refused_whatever_its_case(password: str) -> None:
    identity, store = make()
    refused = identity.login(f"ALICE,{password}")
    assert isinstance(refused, Refused) and "TOO COMMON" in refused.reason
    assert store.count() == 0
    assert isinstance(identity.register("BOB", password), Refused)


@pytest.mark.parametrize("password", ["rangerxyz", "RANGERXYZ", "RangerXyz"])
def test_a_password_equal_to_the_account_name_is_refused(password: str) -> None:
    identity, store = make()
    refused = identity.login(f"rangerxyz,{password}")
    assert isinstance(refused, Refused) and "ACCOUNT NAME" in refused.reason
    assert store.count() == 0


def test_a_common_password_does_not_lock_out_an_account_made_before_the_rule() -> None:
    identity, store = make()
    salt = b"0" * 16
    store.create(Account("OLDTIMER", salt, hash_password("password", salt, FAST), FAST))
    assert pilot(identity.login("OLDTIMER,password")).account == "OLDTIMER"


def test_names_with_digits_underscores_and_dashes_are_fine() -> None:
    identity, _ = make()
    for name in ("ACE_1", "RED-LEADER", "X99"):
        assert isinstance(identity.login(f"{name},sesame"), Pilot)


def test_a_pbkdf2_account_still_logs_in() -> None:
    identity, store = make()
    salt = b"0" * 16
    old = Kdf.pbkdf2(1000)
    store.create(Account("ALICE", salt, hash_password("tangerine2", salt, old), old))
    assert isinstance(identity.login("ALICE,wrong"), Refused)
    assert pilot(identity.login("ALICE,tangerine2")).account == "ALICE"


def test_a_pbkdf2_account_is_rehashed_the_current_way_on_a_right_password() -> None:
    identity, store = make()
    salt = b"0" * 16
    old = Kdf.pbkdf2(1000)
    store.create(Account("ALICE", salt, hash_password("tangerine2", salt, old), old))
    identity.release(pilot(identity.login("ALICE,tangerine2")))
    upgraded = store.get("ALICE")
    assert upgraded is not None and upgraded.kdf == FAST and upgraded.salt != salt
    assert pilot(identity.login("ALICE,tangerine2")).account == "ALICE"


def test_a_current_hash_is_not_rehashed() -> None:
    identity, store = make()
    identity.release(pilot(identity.login("ALICE,tangerine2")))
    before = store.get("ALICE")
    identity.release(pilot(identity.login("ALICE,tangerine2")))
    assert store.get("ALICE") == before


def test_an_account_made_before_the_rules_still_logs_in() -> None:
    identity, store = make()
    salt = b"0" * 16
    store.create(Account("OLD ONE", salt, hash_password("pw", salt, FAST), FAST))
    assert pilot(identity.login("OLD ONE,pw")).account == "OLD ONE"


def test_past_the_cap_no_account_is_registered_but_existing_ones_log_in() -> None:
    store = MemoryAccountStore()
    identity = Identity(store, kdf=FAST, max_accounts=2)
    identity.release(pilot(identity.login("FIRST,sesame")))
    identity.release(pilot(identity.login("SECOND,sesame")))
    refused = identity.login("THIRD,sesame")
    assert isinstance(refused, Refused)
    assert "NO ROOM" in refused.reason
    assert isinstance(identity.login("FIRST,sesame"), Pilot)


def test_with_registration_closed_an_unknown_name_is_refused() -> None:
    store = MemoryAccountStore()
    identity = Identity(store, kdf=FAST, registration="closed")
    refused = identity.login("STRANGER,sesame")
    assert isinstance(refused, Refused)
    assert "NO SUCH ACCOUNT" in refused.reason
    made = identity.register("MEMBER")
    assert not isinstance(made, Refused)
    _, password = made
    assert pilot(identity.login(f"MEMBER,{password}")).account == "MEMBER"
    assert pilot(identity.login("JOINGENIE")).is_guest  # guests are another matter


def test_register_follows_the_same_rules_and_makes_a_password_when_none_is_given() -> None:
    identity, _ = make()
    assert isinstance(identity.register("ADMIN"), Refused)
    made = identity.register("pilot")
    assert not isinstance(made, Refused)
    account, password = made
    assert account.name == "PILOT"
    assert len(password) == policy.GENERATED_PASSWORD_LENGTH
    assert set(password) <= set(policy.GENERATED_PASSWORD_ALPHABET)
    assert isinstance(identity.register("PILOT"), Refused)  # taken


def test_a_reset_password_replaces_the_old_one() -> None:
    identity, _ = make()
    identity.release(pilot(identity.login("ALICE,oldsecret")))
    new = identity.reset_password("alice")
    assert isinstance(new, str)
    assert isinstance(identity.login("ALICE,oldsecret"), Refused)
    assert pilot(identity.login(f"ALICE,{new}")).account == "ALICE"
    assert isinstance(identity.reset_password("NOBODY"), Refused)


def test_unplayed_accounts_are_pruned_but_not_played_or_online_ones() -> None:
    now = [0.0]
    store = MemoryAccountStore(lambda: now[0])
    identity = Identity(store, kdf=FAST)
    for name in ("IDLER", "PLAYER", "ONLINE", "RECENT"):
        identity.release(pilot(identity.login(f"{name},sesame")))
    online = pilot(identity.login("ONLINE,sesame"))  # logs in again at 0: still old enough
    now[0] = 1000.0
    identity.release(pilot(identity.login("RECENT,sesame")))  # touched at 1000
    gone = identity.prune_unplayed(500.0, lambda key: key == "acct:PLAYER")
    assert gone == ["IDLER"]
    assert store.get("IDLER") is None
    assert {n for n in ("PLAYER", "ONLINE", "RECENT") if store.get(n)} == {
        "PLAYER",
        "ONLINE",
        "RECENT",
    }
    identity.release(online)
