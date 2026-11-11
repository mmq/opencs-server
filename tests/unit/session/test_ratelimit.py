"""TokenBucket: bursts, refill, no overdraft."""

from opencs.ratelimit import KeyedBuckets, TokenBucket


def test_a_full_bucket_allows_its_capacity_then_refuses() -> None:
    bucket = TokenBucket(3, 1.0, 0.0)
    assert [bucket.take(0.0) for _ in range(4)] == [True, True, True, False]


def test_it_refills_at_the_rate_up_to_capacity() -> None:
    bucket = TokenBucket(2, 0.5, 0.0)
    assert bucket.take(0.0) and bucket.take(0.0) and not bucket.take(0.0)
    assert not bucket.take(1.0)
    assert bucket.take(2.0)
    assert [bucket.take(1000.0) for _ in range(3)] == [True, True, False]


def test_a_token_given_back_can_be_spent_again_but_never_overfills() -> None:
    bucket = TokenBucket(2, 0.5, 0.0)
    assert bucket.take(0.0) and bucket.take(0.0) and not bucket.take(0.0)
    bucket.give_back(0.0)
    assert bucket.take(0.0) and not bucket.take(0.0)
    bucket.give_back(1000.0)
    assert [bucket.take(1000.0) for _ in range(3)] == [True, True, False]


def test_a_key_s_token_given_back_returns_to_its_own_bucket() -> None:
    buckets: KeyedBuckets[str] = KeyedBuckets(1, 0.0)
    assert buckets.take("a", 0.0) and buckets.take("b", 0.0)
    buckets.give_back("a", 0.0)
    buckets.give_back("gone", 0.0)  # never seen: nothing to return
    assert buckets.take("a", 0.0) and not buckets.take("b", 0.0)
