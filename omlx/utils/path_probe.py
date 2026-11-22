# SPDX-License-Identifier: Apache-2.0
"""Bounded directory probes for model directories on slow or stale mounts.

``Path.exists()`` against a hung NFS/SMB mount blocks until the mount's own
timeout (a minute or more on macOS) and then raises ``TimeoutError``. Startup
touches every configured model directory, so one stale mount can stall boot or
crash it outright. These helpers run the stat in a daemon thread with a hard
deadline and cache the answer briefly, so callers get a prompt yes/no and can
skip the directory instead of waiting on the kernel.

A probe that times out leaves its thread blocked until the kernel gives up;
the cache keeps repeated scans from spawning new ones on every request.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Long enough for a healthy local or warm network dir, short enough that a
# stale mount costs a startup about a second rather than a minute.
DEFAULT_TIMEOUT = 1.5
DEFAULT_TTL = 15.0

_cache: dict[str, tuple[float, bool, str | None]] = {}
_lock = threading.Lock()


def probe_dir(
    path: Path | str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    ttl: float = DEFAULT_TTL,
    refresh: bool = False,
) -> tuple[bool, str | None]:
    """Return ``(available, error)`` for a directory, bounded by ``timeout``.

    ``error`` is ``"does not exist"``, ``"not a directory"``, a formatted
    exception, or ``"timed out after Ns"``. Results are cached for ``ttl``
    seconds unless ``refresh`` is set.
    """
    key = str(path)
    now = time.monotonic()
    if not refresh:
        with _lock:
            cached = _cache.get(key)
        if cached is not None and now - cached[0] < ttl:
            return cached[1], cached[2]

    outcome: list[tuple[bool, str | None]] = []

    def _probe() -> None:
        try:
            candidate = Path(key)
            if not candidate.exists():
                outcome.append((False, "does not exist"))
            elif not candidate.is_dir():
                outcome.append((False, "not a directory"))
            else:
                # Reading one entry surfaces permission and IO errors.
                next(candidate.iterdir(), None)
                outcome.append((True, None))
        except OSError as exc:
            outcome.append((False, f"{type(exc).__name__}: {exc}"))
        except Exception as exc:  # pragma: no cover - defensive
            outcome.append((False, f"{type(exc).__name__}: {exc}"))

    worker = threading.Thread(
        target=_probe,
        name=f"dir-probe-{Path(key).name or 'root'}",
        daemon=True,
    )
    worker.start()
    worker.join(timeout)

    if outcome:
        available, error = outcome[0]
    else:
        available, error = False, f"timed out after {timeout:.1f}s"

    with _lock:
        _cache[key] = (now, available, error)
    return available, error


def forget_dir(path: Path | str | None = None) -> None:
    """Drop cached probe results for one path, or for every path."""
    with _lock:
        if path is None:
            _cache.clear()
        else:
            _cache.pop(str(path), None)
