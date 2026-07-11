"""Pure statistical primitives shared by cross-sectional and time-series research."""
from __future__ import annotations

import random
from typing import Iterable, Sequence


COST_BPS_ONEWAY = 10.0


def avg_ranks(xs: Sequence[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda index: xs[index])
    ranks = [0.0] * len(xs)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and xs[order[end + 1]] == xs[order[index]]:
            end += 1
        rank = (index + end) / 2.0 + 1
        for cursor in range(index, end + 1):
            ranks[order[cursor]] = rank
        index = end + 1
    return ranks


def spearman(a: Sequence[float], b: Sequence[float]) -> float | None:
    if len(a) != len(b) or len(a) < 3:
        return None
    ranks_a, ranks_b = avg_ranks(a), avg_ranks(b)
    count = len(a)
    mean_a, mean_b = sum(ranks_a) / count, sum(ranks_b) / count
    covariance = sum(
        (ranks_a[index] - mean_a) * (ranks_b[index] - mean_b)
        for index in range(count)
    )
    scale_a = sum((value - mean_a) ** 2 for value in ranks_a) ** 0.5
    scale_b = sum((value - mean_b) ** 2 for value in ranks_b) ** 0.5
    return covariance / (scale_a * scale_b) if scale_a and scale_b else None


def mean_t(values: Sequence[float]) -> tuple[float, float, int] | None:
    count = len(values)
    if not count:
        return None
    mean = sum(values) / count
    standard_deviation = (
        sum((value - mean) ** 2 for value in values) / (count - 1)
    ) ** 0.5 if count > 1 else 0.0
    t_value = mean / (standard_deviation / count ** 0.5) if standard_deviation else float("inf")
    return mean, t_value, count


def bootstrap_ci(
    values: Sequence[float],
    n_boot: int = 2000,
    seed: int = 42,
    alpha: float = 0.05,
) -> tuple[float, float] | None:
    count = len(values)
    if count < 3:
        return None
    rng = random.Random(seed)
    means = sorted(
        sum(values[rng.randrange(count)] for _ in range(count)) / count
        for _ in range(n_boot)
    )
    lower = int(alpha / 2 * n_boot)
    upper = int((1 - alpha / 2) * n_boot) - 1
    return means[lower], means[upper]


def block_length_for(count: int) -> int:
    """Rule-of-thumb circular block length ~ n^(1/3), floor 2."""
    return max(2, round(count ** (1.0 / 3.0)))


def _circular_block_mean(
    values: Sequence[float], rng: random.Random, block_length: int
) -> float:
    count = len(values)
    sample_total = 0.0
    taken = 0
    while taken < count:
        start = rng.randrange(count)
        for offset in range(min(block_length, count - taken)):
            sample_total += values[(start + offset) % count]
            taken += 1
    return sample_total / count


def block_bootstrap_ci(
    values: Sequence[float],
    n_boot: int = 2000,
    seed: int = 42,
    alpha: float = 0.05,
    block_length: int | None = None,
) -> tuple[float, float] | None:
    """Circular block bootstrap CI of the mean for serially dependent samples."""
    count = len(values)
    if count < 3:
        return None
    length = block_length or block_length_for(count)
    rng = random.Random(seed)
    means = sorted(
        _circular_block_mean(values, rng, length) for _ in range(n_boot)
    )
    lower = int(alpha / 2 * n_boot)
    upper = int((1 - alpha / 2) * n_boot) - 1
    return means[lower], means[upper]


def block_bootstrap_difference_ci(
    first: Sequence[float],
    second: Sequence[float],
    n_boot: int = 2000,
    seed: int = 42,
    alpha: float = 0.05,
) -> tuple[float, float] | None:
    """Difference-of-means CI resampling each group with circular blocks."""
    if len(first) < 3 or len(second) < 3:
        return None
    length_first = block_length_for(len(first))
    length_second = block_length_for(len(second))
    rng = random.Random(seed)
    differences = sorted(
        _circular_block_mean(first, rng, length_first)
        - _circular_block_mean(second, rng, length_second)
        for _ in range(n_boot)
    )
    lower = int(alpha / 2 * n_boot)
    upper = int((1 - alpha / 2) * n_boot) - 1
    return differences[lower], differences[upper]


def bootstrap_difference_ci(
    first: Sequence[float],
    second: Sequence[float],
    n_boot: int = 2000,
    seed: int = 42,
    alpha: float = 0.05,
) -> tuple[float, float] | None:
    if len(first) < 3 or len(second) < 3:
        return None
    rng = random.Random(seed)
    differences = []
    for _ in range(n_boot):
        first_mean = sum(first[rng.randrange(len(first))] for _ in first) / len(first)
        second_mean = sum(second[rng.randrange(len(second))] for _ in second) / len(second)
        differences.append(first_mean - second_mean)
    differences.sort()
    lower = int(alpha / 2 * n_boot)
    upper = int((1 - alpha / 2) * n_boot) - 1
    return differences[lower], differences[upper]


def sector_demean(triples: Iterable[tuple[float, float, str]]) -> list[tuple[float, float]]:
    materialized = list(triples)
    by_sector: dict[str, list[float]] = {}
    for _, future_return, sector in materialized:
        by_sector.setdefault(sector, []).append(future_return)
    means = {sector: sum(values) / len(values) for sector, values in by_sector.items()}
    counts = {sector: len(values) for sector, values in by_sector.items()}
    return [
        (score, future_return - means[sector])
        for score, future_return, sector in materialized
        if counts[sector] > 1
    ]


def bucket_mean(
    pairs: Iterable[tuple[float, float]], lo: float, hi: float
) -> tuple[float | None, int]:
    selected = [future for score, future in pairs if lo <= score < hi]
    return (sum(selected) / len(selected), len(selected)) if selected else (None, 0)


def quintile_spread(
    pairs: Sequence[tuple[float, float]], cost_bps_oneway: float = COST_BPS_ONEWAY
) -> tuple[float, float, float, float] | None:
    if len(pairs) < 10:
        return None
    ordered = sorted(pairs, key=lambda pair: pair[0])
    size = len(ordered) // 5
    bottom = sum(future for _, future in ordered[:size]) / size
    top = sum(future for _, future in ordered[-size:]) / size
    gross = top - bottom
    net = gross - 4 * cost_bps_oneway / 100.0
    return gross, net, top, bottom
