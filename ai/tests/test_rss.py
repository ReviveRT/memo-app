"""
Reading resident memory out of ``/proc``, including on a machine that has none.

MEMO-22's other measurement. Dollars here are zero, so what actually describes
this design is latency -- which memo_ai/costs.py reads out of the table -- and
memory, which is this.

Every test drives the parsing through a temporary directory rather than the real
``/proc``, because the numbers the real one gives are the numbers being measured
and an assertion about them would be an assertion about the machine. What can be
tested is the arithmetic and the shape of the fallbacks, and the fallbacks are the
part that matters: this suite runs on macOS, where there is no ``/proc`` at all.
"""

import pytest

from memo_ai import rss

# One kernel's worth of the two files, trimmed to the fields that are read. The
# numbers are the ones memo_ai/enrich/local.py measured on a worker holding both
# models: 1,708 MB resident, of which 1,081 MB is the mmap-ed weight file.
STATUS = """\
Name:\tpython3
State:\tS (sleeping)
VmPeak:\t 2515208 kB
VmSize:\t 2489344 kB
VmRSS:\t 1749192 kB
Threads:\t5
"""

SMAPS_ROLLUP = """\
Rss:\t 1749192 kB
Pss:\t 1207312 kB
Shared_Clean:\t 1106944 kB
Shared_Dirty:\t       0 kB
Private_Clean:\t     264 kB
Private_Dirty:\t  641984 kB
"""


@pytest.fixture
def proc(tmp_path, monkeypatch):
    """A stand-in ``/proc/self``, with both files present by default."""
    (tmp_path / "status").write_text(STATUS)
    (tmp_path / "smaps_rollup").write_text(SMAPS_ROLLUP)
    monkeypatch.setattr(rss, "_STATUS", tmp_path / "status")
    monkeypatch.setattr(rss, "_SMAPS_ROLLUP", tmp_path / "smaps_rollup")

    return tmp_path


def test_the_total_and_the_split_are_read_together(proc):
    memory = rss.read()

    assert memory.rss_kb == 1_749_192
    # Shared_Clean + Shared_Dirty, and Private_Clean + Private_Dirty. Both halves
    # of each, because reporting one alone is not a smaller figure, it is a wrong
    # one.
    assert memory.shared_kb == 1_106_944
    assert memory.private_kb == 642_248


def test_the_description_leads_with_the_shared_figure(proc):
    # The shared part is the surprising one and the one that changes a capacity
    # decision: two enriching replicas cost about 2.3 GB between them rather than
    # 3.4, because the weight files are mapped read-only and shared.
    assert rss.describe() == "1,708 MB (1,081 MB shared, 627 MB private)"


def test_a_kernel_without_smaps_rollup_still_reports_a_total(proc):
    # `smaps_rollup` arrived in Linux 4.14. Without it the total is still true and
    # the split is simply unavailable, which is a smaller loss than no reading.
    (proc / "smaps_rollup").unlink()

    assert rss.read().shared_kb is None
    assert rss.describe() == "1,708 MB"


def test_a_partial_smaps_rollup_is_treated_as_no_split_at_all(proc):
    # All-or-nothing on purpose. `Shared_Clean` without `Shared_Dirty` understates
    # exactly the figure this module exists to get right, and an understated shared
    # total is worse than an absent one -- it would make two replicas look more
    # expensive than they are.
    (proc / "smaps_rollup").write_text("Shared_Clean:\t 1106944 kB\n")

    assert rss.read().shared_kb is None


def test_no_proc_at_all_is_a_sentence_rather_than_a_crash(tmp_path, monkeypatch):
    # macOS, and any container that masks /proc. This is decoration on a log line
    # whose real content is that a memo is ready, so it may not be able to fail it.
    monkeypatch.setattr(rss, "_STATUS", tmp_path / "nothing-here")
    monkeypatch.setattr(rss, "_SMAPS_ROLLUP", tmp_path / "nor-here")

    assert rss.read() is None
    assert rss.describe() == "unavailable (no /proc on this platform)"


def test_a_field_this_code_cannot_parse_is_none_rather_than_a_guess(proc):
    # A wrong number in a memory report is worse than no number, because nobody
    # checks a plausible one.
    (proc / "status").write_text("VmRSS:\tlots of it\n")

    assert rss.read() is None
