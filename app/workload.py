"""Bounded synthetic CPU work. Never allocates large buffers or starts subprocesses."""

import time


def burn_cpu(iterations: int, max_seconds: float = 0.25, clock=time.monotonic):
    """Run at most iterations; check a wall-clock budget every 1024 operations.

    This is a cooperative bound, not an OS real-time guarantee. CPU quota or host
    scheduling can delay completion. One request cannot create an infinite loop.
    """
    started = clock()
    checksum = 0
    completed = 0
    for offset in range(0, iterations, 1024):
        if clock() - started >= max_seconds:
            break
        end = min(offset + 1024, iterations)
        for i in range(offset, end):
            checksum = ((checksum * 1664525) + i + 1013904223) & 0xFFFFFFFF
        completed = end
    return completed, checksum
