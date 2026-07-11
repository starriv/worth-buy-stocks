#!/usr/bin/env python3
"""Block bootstrap and shared statistical primitive tests."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backtesting.statistics import (  # noqa: E402
    block_bootstrap_ci,
    block_bootstrap_difference_ci,
    block_length_for,
)


class BlockBootstrapTests(unittest.TestCase):
    def test_block_length_rule_of_thumb(self):
        self.assertEqual(block_length_for(3), 2)
        self.assertEqual(block_length_for(8), 2)
        self.assertEqual(block_length_for(27), 3)
        self.assertEqual(block_length_for(125), 5)

    def test_small_samples_return_none(self):
        self.assertIsNone(block_bootstrap_ci([1.0, 2.0]))
        self.assertIsNone(block_bootstrap_difference_ci([1.0, 2.0], [1.0, 2.0, 3.0]))
        self.assertIsNone(block_bootstrap_difference_ci([1.0, 2.0, 3.0], [1.0]))

    def test_seeded_ci_is_deterministic_and_brackets_the_mean(self):
        values = [1.4, -0.3, 2.2, 0.8, 1.9, -0.5, 2.6, 1.1, 0.4, 1.7]
        first = block_bootstrap_ci(values, seed=42)
        second = block_bootstrap_ci(values, seed=42)
        self.assertEqual(first, second)
        lower, upper = first
        mean = sum(values) / len(values)
        self.assertLess(lower, mean)
        self.assertGreater(upper, mean)

    def test_difference_ci_separates_clearly_distinct_groups(self):
        strong = [4.8, 5.3, 4.6, 5.5, 5.1, 4.9, 5.2, 5.0]
        weak = [-1.1, -0.6, -1.4, -0.8, -1.0, -0.9, -1.2, -0.7]
        lower, upper = block_bootstrap_difference_ci(strong, weak, seed=42)
        self.assertGreater(lower, 0)
        self.assertGreater(upper, lower)

    def test_clustered_series_widens_ci_versus_shuffled(self):
        # Two regimes back-to-back: block resampling must see more mean
        # dispersion than the same values scattered evenly.
        clustered = [3.0, 2.8, 3.2, 3.1, 2.9, -1.0, -1.2, -0.8, -1.1, -0.9]
        scattered = [3.0, -1.0, 2.8, -1.2, 3.2, -0.8, 3.1, -1.1, 2.9, -0.9]
        width = lambda ci: ci[1] - ci[0]  # noqa: E731
        self.assertGreater(
            width(block_bootstrap_ci(clustered, seed=42, block_length=5)),
            width(block_bootstrap_ci(scattered, seed=42, block_length=1)),
        )


if __name__ == "__main__":
    unittest.main()
