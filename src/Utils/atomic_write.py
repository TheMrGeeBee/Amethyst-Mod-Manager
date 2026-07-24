"""
atomic_write.py
Shared `.tmp` → rename helpers used wherever we write a file that must never
be observed half-written (filemap index, profile state, deploy snapshot, …).

Two shapes:

- ``write_atomic`` / ``write_atomic_text`` — caller hands over the full
  payload as bytes or text. Best when the payload is built in memory.
- ``atomic_writer`` — context manager that yields an open temp file. Best
  when the payload is streamed (e.g. walking a directory, writing line by
  line) so we don't have to buffer the whole thing first.

All three create the parent directory, write to ``<path>.tmp`` (or a custom
suffix), and atomically ``rename`` over the destination on success. On
failure, the partial temp file is removed so retries see a clean slate.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path


def _tmp_for(path: Path, *, suffix: str = ".tmp") -> Path:
    """Return a temp sibling for *path*, unique to this call.

    Two writers hitting the SAME destination concurrently (e.g. a background
    install worker and a user-triggered modlist save, both racing to write
    modlist.txt) previously collided on one shared ``<name><suffix>`` path:
    whichever thread's cleanup ``unlink()`` ran on failure could delete the
    OTHER thread's still-in-progress temp file, surfacing as a spurious
    FileNotFoundError that silently dropped the write (the caller usually
    just logs and swallows it) — reproduced under a concurrency stress test
    losing ~70% of writes. The pid+uuid suffix makes every call's temp file
    distinct, so concurrent writers can no longer step on each other; the
    final ``rename`` onto *path* still keeps whichever write lands last,
    same "last write wins" semantics as before, just without the corruption."""
    return path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}{suffix}")


def write_atomic(path: Path, data: bytes, *, suffix: str = ".tmp") -> None:
    """Write *data* to *path* atomically (write-temp → rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path, suffix=suffix)
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_atomic_text(path: Path, text: str, *, encoding: str = "utf-8",
                      errors: str | None = None, suffix: str = ".tmp") -> None:
    """Write *text* to *path* atomically (write-temp → rename).

    ``errors`` is forwarded to ``write_text`` — pass ``"surrogateescape"``
    when *text* may contain filesystem-derived paths with non-UTF-8 bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path, suffix=suffix)
    try:
        tmp.write_text(text, encoding=encoding, errors=errors)
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


@contextmanager
def atomic_writer(path: Path, mode: str = "w", *, encoding: str | None = "utf-8",
                  errors: str | None = None, suffix: str = ".tmp"):
    """Open ``<path><suffix>`` for writing; on clean exit rename it onto *path*.

    ``mode`` follows ``open()`` semantics. For binary mode, pass
    ``encoding=None``. ``errors`` is forwarded to ``open()`` — pass
    ``"surrogateescape"`` when writing filesystem-derived paths that may
    carry non-UTF-8 bytes. On any exception the temp file is removed and the
    original *path* is left untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path, suffix=suffix)
    if "b" in mode:
        fh = tmp.open(mode)
    else:
        fh = tmp.open(mode, encoding=encoding, errors=errors)
    try:
        yield fh
        fh.close()
        tmp.replace(path)
    except BaseException:
        try:
            fh.close()
        except Exception:
            pass
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
