import pytest

from evaluation.reporting import percentile


def test_nearest_rank_includes_tail_in_small_samples():
    assert percentile(list(range(1, 21)), .99) == 20
    assert percentile(list(range(1, 21)), .95) == 19
    assert percentile([4], .5) == 4
    assert percentile([], .99) is None
    with pytest.raises(ValueError):
        percentile([4], 0)
