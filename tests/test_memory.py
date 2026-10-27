"""Allocator settings are safe to apply repeatedly and change no results."""
import numpy as np

from dirmap import memory


def test_allocator_policy_and_trim_are_repeatable_and_keep_live_data():
    live = np.arange(2_000_000, dtype=np.int64)
    for _ in range(2):
        memory.configure()
        scratch = np.ones(6_000_000, np.uint8)  # above the mapping threshold
        del scratch
        memory.release()
    assert int(live.sum()) == 2_000_000*1_999_999//2
