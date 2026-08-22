import unittest

from e2e.scenarios import _matched_relay_delays, _percentile


class RelayLatencyTest(unittest.TestCase):
    def test_matches_identical_frames_and_preserves_duplicate_order(self) -> None:
        first = b"first-frame"
        repeated = b"repeated-frame"
        direct = [
            (first, 1_000_000),
            (repeated, 2_000_000),
            (repeated, 3_000_000),
            (b"direct-only", 4_000_000),
        ]
        relayed = [
            (repeated, 2_400_000),
            (first, 1_250_000),
            (b"relay-only", 8_000_000),
            (repeated, 3_700_000),
        ]

        self.assertEqual(_matched_relay_delays(direct, relayed), [0.25, 0.4, 0.7])

    def test_percentile_uses_nearest_ranked_sample(self) -> None:
        self.assertEqual(_percentile([1.0, 4.0, 2.0, 3.0, 5.0], 0.50), 3.0)
        self.assertEqual(_percentile([1.0, 4.0, 2.0, 3.0, 5.0], 0.95), 5.0)


if __name__ == "__main__":
    unittest.main()
