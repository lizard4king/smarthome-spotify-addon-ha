from __future__ import annotations

import threading

import pytest

from music_alexa.lumi_pairing import LumiPairingObserver, MAX_PAIRS, MAX_OPEN_SECONDS


SKILL = "amzn1.ask.skill.12345678-1234-1234-1234-1234567890ab"
DEVICE = "amzn1.ask.device.echo-01"
USER = "amzn1.ask.account.account-01"


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def observe(observer, *, skill=SKILL, expected=SKILL, device=DEVICE, user=USER):
    return observer.observe_verified_request(
        application_id=skill,
        expected_skill_id=expected,
        device_id=device,
        user_id=user,
    )


def test_closed_by_default_and_requires_exact_validated_skill():
    observer = LumiPairingObserver()
    assert observer.snapshot() == {"open": False, "remaining_seconds": 0, "pairs": []}
    assert not observe(observer)

    observer.open(60)
    assert not observe(observer, skill=SKILL, expected=SKILL + "x")
    assert not observe(observer, skill="wrong", expected="wrong")
    assert not observe(observer, device="not-a-device")
    assert not observe(observer, user="not-a-user")
    assert not observe(observer, device=None)
    assert not observe(observer, device=7)
    assert not observe(observer, user=None)
    assert not observe(observer, user=7)
    assert observer.snapshot()["pairs"] == []


@pytest.mark.parametrize("duration", [0, -1, 901, True, 1.5, "60", None])
def test_open_rejects_invalid_or_overlong_windows(duration):
    observer = LumiPairingObserver()
    with pytest.raises(ValueError):
        observer.open(duration)
    assert observer.snapshot()["open"] is False


def test_window_is_bounded_deduplicated_and_cleared_on_expiry():
    clock = Clock()
    observer = LumiPairingObserver(clock=clock)
    assert observer.open(MAX_OPEN_SECONDS) == MAX_OPEN_SECONDS
    assert observe(observer)
    assert observe(observer)
    assert len(observer.snapshot()["pairs"]) == 1

    for index in range(2, MAX_PAIRS + 1):
        assert observe(observer, device=f"amzn1.ask.device.echo-{index:02d}")
    assert len(observer.snapshot()["pairs"]) == MAX_PAIRS
    assert not observe(observer, device="amzn1.ask.device.echo-overflow")

    clock.now += MAX_OPEN_SECONDS
    assert observer.snapshot() == {"open": False, "remaining_seconds": 0, "pairs": []}
    assert not observe(observer)


def test_close_and_reopen_clear_all_prior_pairs():
    observer = LumiPairingObserver(clock=Clock())
    observer.open(900)
    assert observe(observer)
    observer.close()
    assert observer.snapshot() == {"open": False, "remaining_seconds": 0, "pairs": []}

    observer.open(15)
    assert observer.snapshot()["pairs"] == []
    assert observe(observer, device="amzn1.ask.device.new-echo")
    assert observer.snapshot()["pairs"][0]["device_id"] == "amzn1.ask.device.new-echo"


def test_concurrent_observation_never_exceeds_pair_cap():
    observer = LumiPairingObserver(clock=Clock())
    observer.open(900)
    barrier = threading.Barrier(MAX_PAIRS + 8)
    results = []
    result_lock = threading.Lock()

    def worker(index):
        barrier.wait()
        accepted = observe(observer, device=f"amzn1.ask.device.concurrent-{index}")
        with result_lock:
            results.append(accepted)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(MAX_PAIRS + 8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()

    pairs = observer.snapshot()["pairs"]
    assert len(pairs) == MAX_PAIRS
    assert sum(results) == MAX_PAIRS
    assert all(set(pair) == {"application_id", "device_id", "user_id"} for pair in pairs)
