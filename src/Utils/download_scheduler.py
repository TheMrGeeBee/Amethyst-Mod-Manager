"""Double-ended download dispatch for collection installs (toolkit-neutral).

Problem: with N equal download workers all pulling from one size-sorted list,
many tiny archives finish faster than the Nexus API can hand out the next CDN
link, so the queue stutters and bandwidth sits idle until a big mod happens to
land in a slot.

Fix: dedicate ONE worker to the largest-remaining mods (it stays busy on long
transfers that keep the pipe full) and let the other workers burn through the
smallest-remaining mods from the other end. They converge in the middle, so
every mod is dispatched exactly once and the link-fetch latency of the small
mods is hidden behind the big worker's ongoing transfer.

This module only owns the *dispatch order*; the actual per-mod work (link
fetch, download, hand-off to the install queue) is the caller's `work` fn.
"""

from __future__ import annotations

import threading
from typing import Callable, Iterable


def order_by_size(mods: Iterable, size_key: Callable[[object], int] | None = None
                  ) -> list:
    """Return *mods* sorted smallest→largest by size.

    Mods that don't report a size (``size_bytes`` 0/missing — some Nexus files
    omit it) are sorted to the END, not the front: their real size is unknown and
    could be large, so we download the known-small mods first and leave the
    unknowns for last (rather than letting a big unknown-size mod jump the queue
    and hog a slot while everything small waits behind it)."""
    if size_key is None:
        def size_key(m):
            return getattr(m, "size_bytes", 0) or 0
    # (0 = unknown → sort last) via a (is_unknown, size) key.
    return sorted(mods, key=lambda m: (size_key(m) <= 0, size_key(m)))


def run_smallest_first(mods: list, work: Callable[[object], None], workers: int,
                       *, stop: "threading.Event | None" = None,
                       spawn: Callable[[Callable, str], object] | None = None
                       ) -> None:
    """Dispatch *mods* to *work* strictly smallest→largest across *workers*
    threads, blocking until every mod is processed (or *stop* is set).

    Unlike :func:`run_double_ended`, NO worker is dedicated to large mods: all
    workers pull from the head of the (pre-sorted smallest→largest) list, so the
    smallest remaining mod is always the next one downloaded. NB this is a
    deliberate Qt policy, NOT Tk parity: the Tk installer honoured the
    ``download_order`` collection setting (default "largest" = largest-first);
    Qt ignores that legacy key and always downloads smallest-first.

    *mods*  — PRE-SORTED smallest→largest (see :func:`order_by_size`).
    *stop*  — optional cancel event; when set, workers drain the remainder
              (feeding *work*, which is expected to short-circuit) so the
              caller's per-mod bookkeeping still fires.
    *spawn* — optional ``spawn(target, name) -> thread-like`` for tests.
    """
    n = len(mods)
    if n == 0:
        return
    workers = max(1, int(workers))

    lock = threading.Lock()
    cursor = {"lo": 0, "hi": n - 1}

    def _worker():
        while True:
            if stop is not None and stop.is_set():
                _drain_remaining(work, cursor, lock, mods, stop)
                return
            with lock:
                if cursor["lo"] > cursor["hi"]:
                    return
                mod = mods[cursor["lo"]]
                cursor["lo"] += 1
            work(mod)

    if spawn is None:
        def spawn(target, name):
            return threading.Thread(target=target, name=name, daemon=True)

    threads = [spawn(_worker, f"col-dl-{i}") for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def run_double_ended(mods: list, work: Callable[[object], None], workers: int,
                     *, stop: "threading.Event | None" = None,
                     spawn: Callable[[Callable, str], object] | None = None
                     ) -> None:
    """Dispatch *mods* to *work* using the double-ended policy and block until
    every mod has been processed (or *stop* is set).

    *mods*    — the download units, PRE-SORTED smallest→largest
                (see :func:`order_by_size`).
    *work*    — called once per mod on a worker thread: ``work(mod)``.
    *workers* — total worker threads (>=1). One is the "large" worker pulling
                from the tail; the rest pull from the head.
    *stop*    — optional cancel event; when set, workers drain without calling
                *work* on the remainder (the caller's *work* still runs for
                already-claimed items and is expected to short-circuit on stop).
    *spawn*   — optional ``spawn(target, name) -> thread-like`` with ``.start``/
                ``.join`` (defaults to daemon ``threading.Thread``); lets a test
                inject deterministic threading.

    A single shared [lo, hi] cursor over the sorted list guarantees each mod is
    claimed once. The large worker takes ``mods[hi]`` then hi-=1; the small
    workers take ``mods[lo]`` then lo+=1. When the ranges cross, everyone stops.
    """
    n = len(mods)
    if n == 0:
        return
    workers = max(1, int(workers))

    lock = threading.Lock()
    cursor = {"lo": 0, "hi": n - 1}

    def _claim(from_tail: bool):
        """Claim the next mod for this worker, or None when exhausted."""
        with lock:
            if cursor["lo"] > cursor["hi"]:
                return None
            if from_tail:
                m = mods[cursor["hi"]]
                cursor["hi"] -= 1
            else:
                m = mods[cursor["lo"]]
                cursor["lo"] += 1
            return m

    def _worker(from_tail: bool):
        while True:
            if stop is not None and stop.is_set():
                # Drain the rest without doing work so the caller's join
                # returns promptly; already-claimed items are handled by work.
                _drain_remaining(work, cursor, lock, mods, stop)
                return
            mod = _claim(from_tail)
            if mod is None:
                return
            work(mod)

    if spawn is None:
        def spawn(target, name):
            return threading.Thread(target=target, name=name, daemon=True)

    threads = []
    # Exactly one large-end worker (unless there's only a single worker, in
    # which case it must still cover the whole list — it pulls from the head so
    # small-first behaviour is preserved when workers==1).
    for i in range(workers):
        from_tail = (workers > 1 and i == 0)
        t = spawn(lambda ft=from_tail: _worker(ft),
                  f"col-dl-{'big' if from_tail else 'small'}-{i}")
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def _drain_remaining(work, cursor, lock, mods, stop):
    """After a cancel, feed every remaining mod to *work* so the caller's
    per-mod bookkeeping (counters, install-queue sentinels) still fires — work
    is expected to no-op the actual download when *stop* is set."""
    while True:
        with lock:
            if cursor["lo"] > cursor["hi"]:
                return
            m = mods[cursor["lo"]]
            cursor["lo"] += 1
        work(m)
