"""
How much memory this worker is holding, and how much of it a second replica shares.

MEMO-22's other half. Dollars on this project are zero, so the numbers that
actually describe what this design costs are latency and memory: seconds of
inference per minute of audio, which memo_ai/costs.py reads out of the table, and
resident memory per worker, which is this file.

**Memory is a property of a process, not of a memo**, which is why it is logged
rather than persisted. There is no column it could go in that would mean anything
-- two replicas share one machine, the models load lazily, and the same memo
costs 18 MB on a worker that has not loaded whisper yet and 1.7 GB on one that
has. So the worker states it at boot and on every memo it publishes, and
``python -m memo_ai.costs`` reads its own as a sample.

**The shared/private split is the point, not the total.** Both models here are
``mmap``-ed read-only from files baked into the image -- memo_ai/enrich/local.py
passes ``use_mmap=True`` for exactly this reason -- so a large part of each
replica's RSS is the same physical pages as the other replica's. Reporting only
the total would say two enriching replicas cost 3.4 GB when the measured figure
is about 2.3 GB, and that is the difference between "this needs a big machine"
and "this runs on a laptop". ``smaps_rollup`` is what tells the two apart.

**The split costs 250 times what the total does, so the two are separate calls.**
Measured inside this image on a process holding 1.5 GB, which is roughly a loaded
worker: ``/proc/self/status`` answers in 0.042 ms because ``VmRSS`` is a counter
the kernel already maintains, while ``/proc/self/smaps_rollup`` takes **10.8 ms**
because it walks the process's page tables to produce it -- and that number grows
with resident memory, so it is worst exactly on the worker that most wants
measuring. :func:`brief` therefore reads the counter and :func:`describe` does the
walk, and the caller picks: the worker states the full split once at boot, when it
is holding 18 MB and the walk is free, and reports the cheap total on every memo
after that.

Everything here degrades to ``None`` off Linux rather than raising. The tests run
on macOS, where there is no ``/proc`` at all, and a memory reading is a nice-to-
have on a line whose real content is that a memo is ready.
"""

from dataclasses import dataclass
from pathlib import Path

# Linux's process accounting, both in kB. `status` is the portable one -- it has
# existed forever and `VmRSS` is in every kernel -- while `smaps_rollup` arrived
# in 4.14 and is what carries the shared/private breakdown. Read separately so
# that a kernel without the second still reports a total.
_STATUS = Path("/proc/self/status")
_SMAPS_ROLLUP = Path("/proc/self/smaps_rollup")

_KB_PER_MB = 1024


@dataclass(frozen=True)
class Memory:
    """
    One process's resident set, split by what another process could be sharing.

    ``shared_kb`` is ``Shared_Clean`` plus ``Shared_Dirty`` from
    ``smaps_rollup``: pages this process has resident that are also mapped by at
    least one other. For this worker that is overwhelmingly the two weight files,
    and it was checked rather than assumed -- bringing up a second replica against
    the same image reports the enrichment model's 1,081 MB as ``Shared_Clean``
    with ``Private_Clean`` at zero.

    ``private_kb`` is the part that genuinely doubles when a replica does: the KV
    cache, the decode buffers, the interpreter. It is the number to multiply by
    ``replicas:``.

    Both are ``None`` on a kernel with no ``smaps_rollup``, where the total is
    still true and the split is simply not available.
    """

    rss_kb: int
    shared_kb: int | None = None
    private_kb: int | None = None

    def rss_mb(self) -> float:
        return self.rss_kb / _KB_PER_MB

    def describe(self) -> str:
        """
        ``1,708 MB (1,081 MB shared, 627 MB private)`` -- one line, for a log.

        The shared figure first, because it is the surprising one and the one that
        changes a capacity decision.
        """
        if self.shared_kb is None or self.private_kb is None:
            return f"{self.rss_mb():,.0f} MB"

        return (
            f"{self.rss_mb():,.0f} MB "
            f"({self.shared_kb / _KB_PER_MB:,.0f} MB shared, "
            f"{self.private_kb / _KB_PER_MB:,.0f} MB private)"
        )


def read(split: bool = True) -> Memory | None:
    """
    This process's resident set, or ``None`` where the kernel will not say.

    ``split=False`` skips ``smaps_rollup`` entirely and returns the total alone.
    That is not a micro-optimisation: the walk is 10.8 ms against the counter's
    0.042 ms on a loaded worker, and it is the difference between a per-memo log
    line that is free and one that is not. The module docstring has the numbers.

    ``None`` rather than a zero or a raise. Zero would be a lie that averages into
    something, and raising would mean every caller wrapping a diagnostic in a
    try-block -- for a number that is decoration on a log line and a footnote in a
    report.
    """
    rss_kb = _field(_STATUS, "VmRSS:")

    if rss_kb is None:
        return None

    if not split:
        return Memory(rss_kb=rss_kb)

    shared = _sum_of(_SMAPS_ROLLUP, ("Shared_Clean:", "Shared_Dirty:"))
    private = _sum_of(_SMAPS_ROLLUP, ("Private_Clean:", "Private_Dirty:"))

    return Memory(rss_kb=rss_kb, shared_kb=shared, private_kb=private)


def describe() -> str:
    """
    The full reading as a phrase, split included. Pays for the page-table walk.

    For the places a reading is taken once -- the worker's boot line, a shell.
    :func:`brief` is what belongs on anything per-memo.
    """
    return _phrase(read())


def brief() -> str:
    """The total alone, cheap enough for a line that runs on every memo."""
    return _phrase(read(split=False))


def _phrase(memory: "Memory | None") -> str:
    return "unavailable (no /proc on this platform)" if memory is None else memory.describe()


def _field(path: Path, label: str) -> int | None:
    """
    The kB value on the line starting with ``label``, or ``None``.

    Every one of these files is `Label:   <number> kB`, so the value is the
    second-to-last field. Parsed that way rather than by splitting on the colon,
    because the whitespace between the colon and the number is not fixed.
    """
    for line in _lines(path):
        if line.startswith(label):
            parts = line.split()

            # `VmRSS:` is `['VmRSS:', '18040', 'kB']`. Anything else is a kernel
            # that formats this differently, and a wrong number is worse than none.
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])

            return None

    return None


def _sum_of(path: Path, labels: tuple[str, ...]) -> int | None:
    """
    The named fields added together, or ``None`` if any of them is missing.

    All-or-nothing on purpose: ``Shared_Clean`` without ``Shared_Dirty`` is not a
    smaller shared total, it is an incomplete one, and reporting it as the answer
    would understate exactly the figure this module exists to get right.
    """
    values = [_field(path, label) for label in labels]

    return None if any(value is None for value in values) else sum(values)


def _lines(path: Path) -> list[str]:
    """
    The file's lines, or none of them.

    ``OSError`` covers all three ways this fails and they are all the same
    non-event: no ``/proc`` on macOS (``FileNotFoundError``), a hardened container
    that masks it (``PermissionError``), and a kernel too old for
    ``smaps_rollup``. None of them is worth a log line of its own -- the caller
    already prints "unavailable" once.
    """
    try:
        return path.read_text().splitlines()
    except OSError:
        return []
