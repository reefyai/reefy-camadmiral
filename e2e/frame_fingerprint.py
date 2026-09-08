from __future__ import annotations

import math


FRAME_WIDTH = 32
FRAME_HEIGHT = 18
FRAME_CHANNELS = 3
FRAME_SIZE = FRAME_WIDTH * FRAME_HEIGHT * FRAME_CHANNELS


def frame_fingerprint(frame: bytes) -> list[float]:
    if len(frame) != FRAME_SIZE:
        raise ValueError("Decoded frame has an unexpected size")
    sums = [[0, 0, 0] for _ in range(4)]
    counts = [0, 0, 0, 0]
    for y in range(FRAME_HEIGHT):
        for x in range(FRAME_WIDTH):
            quadrant = (2 if y >= FRAME_HEIGHT // 2 else 0) + (
                1 if x >= FRAME_WIDTH // 2 else 0
            )
            offset = (y * FRAME_WIDTH + x) * FRAME_CHANNELS
            for channel in range(FRAME_CHANNELS):
                sums[quadrant][channel] += frame[offset + channel]
            counts[quadrant] += 1
    return [
        round(sums[quadrant][channel] / counts[quadrant], 3)
        for quadrant in range(4)
        for channel in range(FRAME_CHANNELS)
    ]


def mean_fingerprint(fingerprints: list[list[float]]) -> list[float]:
    if not fingerprints:
        raise ValueError("At least one fingerprint is required")
    width = len(fingerprints[0])
    if width == 0 or any(len(item) != width for item in fingerprints):
        raise ValueError("Fingerprints must have equal non-zero lengths")
    return [
        round(sum(item[index] for item in fingerprints) / len(fingerprints), 3)
        for index in range(width)
    ]


def fingerprint_distance(first: list[float], second: list[float]) -> float:
    if not first or len(first) != len(second):
        raise ValueError("Fingerprints must have equal non-zero lengths")
    return math.sqrt(
        sum((left - right) ** 2 for left, right in zip(first, second, strict=True))
        / len(first)
    )
