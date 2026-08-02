"""
The cost report: what it asks the database for, and what it says about the answer.

Not whether the SQL is right. Three of these statements use ``percentile_cont``,
``FILTER`` and ``round(...)::bigint``, none of which an in-memory substitute would
evaluate honestly -- the same split tests/test_memos.py makes, for the same
reason. They were run against a real Postgres over a mixed set of short and long
memos, and memo_ai/costs.py records what those runs showed.

What is left here is everything around them, and it is where a mistake would be
silent: the rates bound into the projection, and the sentences the report prints.
**The output is the deliverable for MEMO-22** -- the task is "answer this on a
call" -- so a format string that rounded a real cost to $0.00 would fail the task
while passing every other test in this suite.
"""

import re

import pytest

from memo_ai import costs, rates

# One row of each statement, shaped like a stack that has taken a mixed set: some
# short memos, some long ones, and a couple of text memos that never had audio.
#
# The numbers are internally consistent rather than arbitrary, because two of the
# assertions below are arithmetic on them: 340 minutes at whisper-1's $0.006 is
# $2.04, and 812,000 + 97,000 tokens at gpt-4o-mini's rates is $0.1800.
SPEND_ROW = {
    "memos": 1_000,
    "transcribed": 900,
    "recordings": 800,
    "enrichments": 1_000,
    "shortest_ms": 4_100,
    "longest_ms": 552_000,
    "audio_minutes": 340.0,
    "input_tokens": 812_000,
    "output_tokens": 97_000,
    "measured_micro_usd": 0,
    "projected_stt_micro_usd": 2_040_000,
    "projected_enrich_micro_usd": 180_200,
}

TRANSCRIPTION_ROW = {
    "memos": 800,
    "median_seconds_per_minute": 38.4,
    "p95_seconds_per_minute": 71.2,
    "median_realtime_factor": 0.64,
    "inference_seconds": 13_056.0,
    "audio_minutes": 340.0,
}

ENRICHMENT_ROW = {
    "memos": 1_000,
    "counted": 1_000,
    "median_seconds": 13.2,
    "p95_seconds": 36.2,
    "median_tokens_per_second": 69.0,
    "inference_seconds": 13_200.0,
}

EMPTY_LATENCY = {"memos": 0}


class StubCursor:
    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False

    def execute(self, sql, params=None):
        self._connection.executed.append((sql, params))

    def fetchone(self):
        return self._connection.rows.pop(0)


class StubConnection:
    """
    Hands back one canned row per statement, in the order they are run.

    Purpose-built rather than reusing ``tests.support.FakeConnection``, which
    answers every ``fetchone`` with the same row. This report runs three different
    statements and the whole question is whether each result reaches the right part
    of the output, so they have to be distinguishable.
    """

    def __init__(self, rows):
        self.executed = []
        self.rows = list(rows)

    def cursor(self, row_factory=None):
        # `row_factory` accepted and ignored: the real one is psycopg's `dict_row`,
        # and these rows are already dicts.
        return StubCursor(self)

    def params_for(self, fragment):
        matches = [params for sql, params in self.executed if fragment in sql]

        assert len(matches) == 1, f"expected one statement containing {fragment!r}"

        return matches[0]


def connection(spend=None, transcription=None, enrichment=None):
    return StubConnection(
        [
            SPEND_ROW if spend is None else spend,
            TRANSCRIPTION_ROW if transcription is None else transcription,
            ENRICHMENT_ROW if enrichment is None else enrichment,
        ]
    )


# ---------------------------------------------------------------------------
# What the statements are given
# ---------------------------------------------------------------------------


def test_the_projection_binds_the_rate_rather_than_hard_coding_one():
    # The rates are parameters, not literals in the SQL, which is what makes
    # `--stt-model` work at all and what keeps the rate table the single place a
    # price is written down.
    stub = connection()

    costs.collect(stub, stt_model="whisper-1", enrich_model="gpt-4o-mini")

    params = stub.params_for("projected_stt_micro_usd")

    # Micro-dollars per unit, so the SQL multiplies rather than converting. $0.006
    # a minute is 6,000 micro-dollars a minute; $0.15 per million input tokens is
    # 0.15 micro-dollars a token.
    assert params["stt_micro_usd_per_minute"] == 6_000
    assert params["input_micro_usd_per_token"] == pytest.approx(0.15)
    assert params["output_micro_usd_per_token"] == pytest.approx(0.60)


def test_the_two_latency_statements_take_no_parameters():
    # They measure, they do not price. A rate reaching one of them would mean the
    # median had been scaled by something.
    stub = connection()

    costs.collect(stub)

    assert stub.params_for("median_seconds_per_minute") == {}
    assert stub.params_for("median_tokens_per_second") == {}


def test_an_unknown_model_is_refused_before_any_statement_runs():
    # A typo should cost a dict lookup, not three sequential scans of the memos
    # table followed by an error.
    stub = connection()

    with pytest.raises(ValueError, match="Known models:"):
        costs.collect(stub, stt_model="gpt-9-omni")

    assert stub.executed == []


def test_the_audio_sum_excludes_memos_that_were_never_transcribed():
    # `duration_ms` is written on failure paths too -- a memo refused as too long
    # carries its length so the UI can show it -- and a hosted provider is not paid
    # for a request that was never made. Without this filter the projection
    # over-states by exactly the memos this stack declined to send. On the mixed set
    # this was verified against, that was 27.6 audio-minutes against a true 12.6.
    assert "sum(duration_ms) FILTER (WHERE transcript IS NOT NULL)" in costs.SPEND


def test_an_enriched_memo_is_counted_by_its_provider_not_by_its_tokens():
    # A binding that reports no usage still ran and still billed, so counting on a
    # token column undercounts the enrichments -- verified against Postgres, where a
    # pair of timed memos with NULL counts reported `enriched 0`.
    assert "count(*) FILTER (WHERE enrich_provider IS NOT NULL)" in costs.SPEND


def test_the_throughput_median_skips_runs_that_reported_no_tokens():
    # The correction that matters most in this file. Coalescing the counts to zero
    # put "not measured" into the median as the slowest possible measurement:
    # against Postgres, two reporting rows at 300 and 100 tokens/s beside two
    # non-reporting ones gave 50 tokens/s where the truth is 200.
    assert "FILTER (WHERE enrich_input_tokens IS NOT NULL" in costs.ENRICHMENT_LATENCY
    assert "coalesce(enrich_input_tokens" not in costs.ENRICHMENT_LATENCY


def test_the_latency_queries_cannot_divide_by_zero():
    # `duration_ms` is the divisor. Nothing produces a zero-length recording today
    # -- memo_ai/audio.py refuses a file with no audio stream first -- and a
    # division by zero in a report only ever shows up in front of somebody.
    assert "duration_ms > 0" in costs.TRANSCRIPTION_LATENCY
    assert "enrich_ms > 0" in costs.ENRICHMENT_LATENCY


# ---------------------------------------------------------------------------
# What the report says
# ---------------------------------------------------------------------------


def report(**overrides):
    fields = {
        "spend": SPEND_ROW,
        "transcription": TRANSCRIPTION_ROW,
        "enrichment": ENRICHMENT_ROW,
        "stt_model": "whisper-1",
        "enrich_model": "gpt-4o-mini",
        "per": 1_000,
    }

    return costs.Report(**(fields | overrides))


def rendered(**overrides):
    """
    The report with its column padding collapsed to single spaces.

    So that the assertions below can bind a label to the value beside it -- which
    is the property that matters, since a figure under the wrong heading is worse
    than a misaligned one -- without breaking every time the column width moves.
    """
    return re.sub(r"[ \t]+", " ", costs.render(report(**overrides)))


def test_measured_spend_is_reported_as_zero_and_read_rather_than_asserted():
    # Half of MEMO-22's first acceptance criterion. The value comes from
    # `sum(cost_micro_usd)`, so the day somebody wires up a hosted provider this
    # line changes on its own -- which is the one day it matters.
    text = rendered()

    assert "charged by a provider $0.0000" in text
    assert "nothing" in text and "was billed" in text


def test_the_hosted_projection_is_not_zero_and_names_the_rate_behind_it():
    # The other half. A projection with no rate beside it is a number nobody can
    # check, and these are list prices rather than an invoice.
    text = rendered()

    assert "340.0 audio-minutes $2.0400" in text
    assert "909,000 tokens $0.1802" in text
    assert "rate $0.0060 per audio-minute" in text
    assert f"rates checked {rates.RATES_CHECKED}" in text


def test_the_total_is_restated_per_thousand_memos():
    # The sentence the task wants somebody to be able to say out loud. This set is
    # already 1,000 memos, so the per-1000 figure equals the total -- which is the
    # arithmetic being checked, not a coincidence to work around.
    text = rendered()

    assert "total for these 1,000 memos $2.2202" in text
    assert "per 1,000 memos $2.2202" in text


def test_the_per_thousand_figure_scales_a_smaller_sample_up():
    # The case that actually exercises the division: ten memos of this shape
    # projected to a thousand is a hundred times the money.
    assert "per 1,000 memos $222.0200" in rendered(spend=SPEND_ROW | {"memos": 10})


def test_an_empty_table_does_not_divide_by_zero():
    # Reachable the moment anybody runs this before recording anything, which is
    # the first thing a reader of the README will do.
    empty = SPEND_ROW | {
        "memos": 0,
        "transcribed": 0,
        "recordings": 0,
        "enrichments": 0,
        "shortest_ms": None,
        "longest_ms": None,
        "audio_minutes": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "measured_micro_usd": 0,
        "projected_stt_micro_usd": 0,
        "projected_enrich_micro_usd": 0,
    }
    text = rendered(spend=empty, transcription=EMPTY_LATENCY, enrichment=EMPTY_LATENCY)

    assert "per 1,000 memos no memos yet" in text
    assert "transcription no timed transcriptions yet" in text
    assert "enrichment no timed enrichments yet" in text


def test_the_spread_shows_the_set_was_mixed():
    # MEMO-22's acceptance is specifically "a mixed set of short and long memos",
    # and this is where a reader checks that it was.
    assert "4s to 9m 12s" in rendered()


def test_the_median_inference_rate_is_reported_in_both_units():
    # The second acceptance query. Seconds per audio-minute is what capacity
    # planning wants; the realtime factor is how whisper's speed is discussed
    # everywhere else in this repo, and the two are the same measurement.
    text = rendered()

    assert "median 38.4s of inference per audio-minute (0.64x realtime)" in text
    assert "p95 71.2s per audio-minute" in text


def test_the_cumulative_inference_time_is_reported_in_a_readable_unit():
    # Both totals used to be selected and never printed. They are the figure that
    # is about the machine rather than about a memo, and 13,056 seconds is not a
    # number anybody converts in their head.
    text = rendered()

    assert "total 3.6 hours of inference" in text


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "0.0s of inference"),
        (12.8, "12.8s of inference"),
        (59.9, "59.9s of inference"),
        (60.0, "1.0 minutes of inference"),
        (3_599.0, "60.0 minutes of inference"),
        (3_600.0, "1.0 hours of inference"),
        (None, "not measured"),
    ],
)
def test_a_total_picks_the_unit_that_keeps_it_readable(seconds, expected):
    assert costs._duration(seconds) == expected


def test_a_set_where_nothing_reported_tokens_prints_no_throughput():
    # And does not crash, which is the failure this branch exists for: with the
    # coalesce removed, `median_tokens_per_second` is NULL for such a set, and
    # `f"{None:,.0f}"` is a TypeError in the middle of printing the report.
    quiet = ENRICHMENT_ROW | {"counted": 0, "median_tokens_per_second": None}
    text = rendered(enrichment=quiet)

    assert "median 13.2s\n" in text
    assert "tokens/s" not in text


def test_the_footer_points_at_the_workers_rather_than_sampling_this_process():
    # RAM belongs to a process, and the only one this command can read is its own
    # -- a bare `python -m memo_ai.costs`, about 35 MB. Printing that under a
    # heading about what the design costs invited exactly one misreading: that a
    # worker holding two models costs 35 MB. So there is no live number here.
    text = rendered()

    assert "resident memory per worker, in the worker's own log" in text
    assert "docker compose logs ai-worker | grep rss" in text
    assert "35 MB" not in text


def test_render_reads_nothing_but_its_report():
    # The docstring on `render` claims it is a function of the Report and nothing
    # else, and it was not: it called `rss.describe()` itself, so its output
    # depended on the machine printing it. Two identical reports must render
    # identically.
    assert costs.render(report()) == costs.render(report())


# ---------------------------------------------------------------------------
# The column, and the arguments
# ---------------------------------------------------------------------------


def test_a_label_wider_than_the_column_keeps_a_space_before_its_value():
    # `_LABEL_WIDTH` is sized for a thousand memos and the label grows with the
    # count, so a plain ljust welds the number onto the word at a million:
    # `total for these 1,000,000 memos$2.0000`.
    line = costs._row("total for these 1,000,000 memos", "$2.0000")

    assert line == "  total for these 1,000,000 memos $2.0000"


def test_values_line_up_in_one_column_whatever_the_indent():
    # The only formatting a `docker compose run` log preserves.
    assert costs._row("memos", "10").index("10") == costs._row("p95", "39.2s", indent=4).index(
        "39.2s"
    )


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "1.5"])
def test_a_nonsensical_per_is_refused_rather_than_printed(bad):
    # Neither degenerate value fails loudly on its own: `--per 0` prints a
    # projection of $0.0000, which reads as "this is free", and a negative one
    # prints negative money.
    with pytest.raises(SystemExit):
        costs.main(["--per", bad])


def test_the_rate_table_prints_without_a_database():
    # `--rates` is the one path that must work on a laptop with nothing running,
    # because it is how somebody checks a number before quoting it.
    text = costs._rate_table()

    for model in list(rates.STT_RATES) + list(rates.ENRICH_RATES):
        assert model in text

    assert "has ever been charged" in text


def test_the_rates_flag_touches_nothing_and_exits_clean():
    assert costs.main(["--rates"]) == 0
