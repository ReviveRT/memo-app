"""
What this stack has spent, what a hosted one would have, and how slow it is.

MEMO-22's deliverable, and it is a report rather than a feature: three aggregates
over ``memos``, printed by ``python -m memo_ai.costs``. Nothing in the request
path reads this module, nothing imports it at boot, and removing it would change
no behaviour -- which is the correct weight for a thing whose entire job is to let
somebody answer a question on a call.

The question is "what would this cost per 1000 memos?", and the honest answer for
this project has two halves that have to be given together:

  * **measured spend is zero**, on every row, because every model runs in this
    container. :data:`SPEND` reads ``memos.cost_micro_usd`` rather than asserting
    that -- a report that hard-coded $0 would still say $0 the day somebody wired
    up a hosted provider, which is the one day it matters.
  * **the projection is not zero**, and it is computed from measurements that cost
    nothing to take: minutes of audio, tokens in, tokens out.
    db/migrations/006_cost_accounting.sql persists them and memo_ai/rates.py has
    the rates.

The third aggregate is the one that describes the design that was actually built.
Since dollars here are zero, the real constraints are latency and memory, so
:data:`TRANSCRIPTION_LATENCY` reports **median seconds of inference per minute of
audio** -- the number that answers "would this scale?" for a CPU-bound local
model. Resident memory is the other half of that answer and is not in this file:
it is a property of a *process*, not of a row, so the worker logs it (see
memo_ai/rss.py) and the footer below says where to read it.

**Every statement here is a read-only aggregate over the whole table.** No
indexes were added for them (006 says why), no filters are applied that a caller
did not ask for, and they are safe to run against a live stack -- an aggregate
takes no locks the claim loop can feel.
"""

import argparse
import sys
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from memo_ai import db, rates
from memo_ai.config import ConfigError, Settings

# Exit code for a bad `--stt-model` or a missing DATABASE_URL, matching the
# worker's for a bad variable.
EXIT_MISUSED = 2

# And for a database that is not there, which is a different thing entirely: the
# command was right and the stack is down. Distinct so that a script wrapping this
# can retry one and not the other.
EXIT_UNAVAILABLE = 3

# ---------------------------------------------------------------------------
# The statements
# ---------------------------------------------------------------------------

# Measured spend, the usage behind it, and what the same usage would cost hosted.
#
# One row, over every memo, and the counts beside the sums are what make it
# readable: a projection of $2.00 means nothing without knowing it came from 340
# minutes of audio across 1,000 memos.
#
# **`FILTER (WHERE transcript IS NOT NULL)` on the audio sum is the one predicate
# with an argument behind it.** `duration_ms` is written on failure paths too --
# memo_ai/pipeline.py persists it for a memo refused as too long, precisely so the
# UI can show a length beside the refusal -- and a hosted provider is not paid for
# a request that was never made. Summing every measured duration would therefore
# over-project by exactly the memos this stack declined to send. Text memos need
# no such filter: they have no audio and their `duration_ms` is NULL, so they fall
# out of the sum on their own while still counting toward `memos` below, which is
# what makes the per-1000 figure describe a realistic mix rather than a voice-only
# one.
#
# The token sums need no filter for the same reason in reverse: those columns are
# written only when an enricher actually generated something.
#
# `round(...)::bigint` rather than leaving a float: the unit is the micro-dollar
# and a fractional micro-dollar is not a thing. Rounding in SQL rather than in
# Python keeps the projected columns the same type as `measured_micro_usd`, so the
# printing code cannot format one of them differently from the other.
SPEND = """
    SELECT
        count(*)                                            AS memos,
        count(*) FILTER (WHERE transcript IS NOT NULL)      AS transcribed,
        count(*) FILTER (WHERE duration_ms IS NOT NULL
                           AND transcript IS NOT NULL)      AS recordings,

        -- `enrich_provider IS NOT NULL` is "an enricher ran on this memo", which is
        -- the question a cost report asks -- not `enriched_at IS NOT NULL`, which is
        -- "it produced something worth showing", and not a token count, which is the
        -- first version of this line and was wrong twice over. A binding that reports
        -- no usage still ran and still billed; and `> 0` on a nullable column is NULL
        -- for every memo nothing enriched, which `count(*) FILTER` reads as false and
        -- so happens to give the right answer for the wrong reason.
        count(*) FILTER (WHERE enrich_provider IS NOT NULL) AS enrichments,

        -- Rounded to the millisecond as stored, then divided once, so the shortest
        -- and longest memo in the set are visible beside the total they average to.
        -- "A mixed set of short and long memos" is MEMO-22's acceptance condition,
        -- and this is where a reader checks that the set really was mixed.
        min(duration_ms) FILTER (WHERE transcript IS NOT NULL)  AS shortest_ms,
        max(duration_ms) FILTER (WHERE transcript IS NOT NULL)  AS longest_ms,

        coalesce(sum(duration_ms) FILTER (WHERE transcript IS NOT NULL), 0)
            / 60000.0                                       AS audio_minutes,
        coalesce(sum(enrich_input_tokens), 0)               AS input_tokens,
        coalesce(sum(enrich_output_tokens), 0)              AS output_tokens,

        -- What anybody was actually charged. Zero on every local run, and read
        -- rather than assumed.
        coalesce(sum(cost_micro_usd), 0)                    AS measured_micro_usd,

        round(
            coalesce(sum(duration_ms) FILTER (WHERE transcript IS NOT NULL), 0)
                / 60000.0 * %(stt_micro_usd_per_minute)s
        )::bigint                                           AS projected_stt_micro_usd,

        round(
            coalesce(sum(enrich_input_tokens), 0) * %(input_micro_usd_per_token)s
          + coalesce(sum(enrich_output_tokens), 0) * %(output_micro_usd_per_token)s
        )::bigint                                           AS projected_enrich_micro_usd
    FROM memos
"""

# Median seconds of inference per minute of audio. MEMO-22's second acceptance query.
#
# `stt_ms * 60.0 / duration_ms` is the whole of it: milliseconds of inference over
# milliseconds of audio is the realtime factor, and sixty times that is seconds per
# audio-minute. The two are the same measurement in different units and both are
# reported, because the realtime factor is how whisper's speed is discussed
# everywhere else in this repo (memo_ai/config.py quotes 0.64x for the shipped
# model) while seconds-per-audio-minute is what capacity planning needs.
#
# `percentile_cont` rather than `avg`, and that is not a stylistic preference. This
# distribution has a long right tail -- a reviewer's laptop under load, a cold page
# cache, a replica descheduled by its peer -- and one such row moves a mean on a
# small sample while leaving a median where it belongs. `percentile_cont`
# interpolates between the two middle rows, which is the right choice for a
# continuous quantity; `percentile_disc` would snap to whichever row happened to
# be there.
#
# `duration_ms > 0` rather than `IS NOT NULL`, because it is a divisor. A
# zero-length recording is not reachable today -- memo_ai/audio.py refuses a file
# with no audio stream before any provider sees it -- and a division by zero in a
# report is the kind of thing that only shows up in front of somebody.
TRANSCRIPTION_LATENCY = """
    SELECT
        count(*)                                                AS memos,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY stt_ms * 60.0 / duration_ms)
                                                                AS median_seconds_per_minute,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY stt_ms * 60.0 / duration_ms)
                                                                AS p95_seconds_per_minute,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY stt_ms * 1.0 / duration_ms)
                                                                AS median_realtime_factor,
        sum(stt_ms) / 1000.0                                    AS inference_seconds,
        sum(duration_ms) / 60000.0                              AS audio_minutes
    FROM memos
    WHERE stt_ms IS NOT NULL
      AND duration_ms > 0
"""

# The same question for the second stage, where there is no audio to divide by.
#
# Enrichment's cost does not scale with a recording's length -- it scales with the
# transcript in front of the model, and a text memo has one without ever having
# been audio. So this reports seconds outright plus tokens per second, which is the
# figure that transfers to another machine: memo_ai/enrich/local.py's measured
# 2.4s / 13.2s / 36.2s are wall-clock numbers from one laptop, and a throughput is
# what says whether a different one would be better or worse.
#
# Beyond MEMO-22's stated acceptance, which asks for the transcription figure
# alone. It is included because it costs one more statement and because the
# enrichment pass is the half of this pipeline whose latency a reviewer actually
# notices -- the transcript is already on screen by the time it runs.
#
# **The throughput is FILTERed to rows that reported tokens, and that is a
# correction rather than a refinement.** The first version coalesced the two counts
# to zero, so a memo timed by an enricher whose binding reports no usage entered the
# median as *0 tokens per second* -- a row that means "not measured" arriving as the
# slowest possible measurement, and dragging down the one figure that is supposed to
# transfer to another machine. NULL from the coalesce would have been no better,
# because a set where nothing reported usage would then hand `render` a None to
# format. The filter states the intent: this median is over the runs that can
# answer, and `memos` above says how many there were in total.
ENRICHMENT_LATENCY = """
    SELECT
        count(*)                                            AS memos,
        count(*) FILTER (WHERE enrich_input_tokens IS NOT NULL
                           AND enrich_output_tokens IS NOT NULL)
                                                            AS counted,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY enrich_ms / 1000.0)
                                                            AS median_seconds,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY enrich_ms / 1000.0)
                                                            AS p95_seconds,
        percentile_cont(0.5) WITHIN GROUP (
            ORDER BY (enrich_input_tokens + enrich_output_tokens) * 1000.0 / enrich_ms
        ) FILTER (WHERE enrich_input_tokens IS NOT NULL
                    AND enrich_output_tokens IS NOT NULL)   AS median_tokens_per_second,
        sum(enrich_ms) / 1000.0                             AS inference_seconds
    FROM memos
    WHERE enrich_ms > 0
"""


@dataclass(frozen=True)
class Report:
    """
    The three result rows, and the two rates they were priced with.

    Carried together rather than printed as they are fetched, so that
    :func:`render` is a pure function of this object and the tests can assert on
    the sentences a person reads without a database. That matters more than usual
    here: the output *is* the deliverable, and "the projection printed $0.00
    because the format string had two decimal places" is a bug this report exists
    to not have.
    """

    spend: dict[str, Any]
    transcription: dict[str, Any]
    enrichment: dict[str, Any]
    stt_model: str
    enrich_model: str
    per: int


def collect(
    connection,
    stt_model: str = rates.DEFAULT_STT_MODEL,
    enrich_model: str = rates.DEFAULT_ENRICH_MODEL,
    per: int = 1000,
) -> Report:
    """
    Run the three statements and price them against the named hosted models.

    The rates are resolved *before* the first statement runs, so a typo in
    ``--stt-model`` costs a lookup rather than three table scans and an error
    afterwards.
    """
    audio_rate = rates.stt_rate(stt_model)
    token_rate = rates.enrich_rate(enrich_model)

    return Report(
        spend=_one(
            connection,
            SPEND,
            {
                "stt_micro_usd_per_minute": audio_rate.micro_usd_per_minute(),
                "input_micro_usd_per_token": token_rate.micro_usd_per_input_token(),
                "output_micro_usd_per_token": token_rate.micro_usd_per_output_token(),
            },
        ),
        transcription=_one(connection, TRANSCRIPTION_LATENCY, {}),
        enrichment=_one(connection, ENRICHMENT_LATENCY, {}),
        stt_model=stt_model,
        enrich_model=enrich_model,
        per=per,
    )


def render(report: Report) -> str:
    """
    The report as text, which is the whole user interface.

    Plain lines rather than a table library, for the reason the rest of this
    project avoids dependencies it can do without: the output is read in
    ``docker compose run`` output where column alignment is the only formatting
    that survives anyway.
    """
    spend = report.spend
    audio_rate = rates.stt_rate(report.stt_model)
    token_rate = rates.enrich_rate(report.enrich_model)

    memos = spend["memos"]
    projected = spend["projected_stt_micro_usd"] + spend["projected_enrich_micro_usd"]

    lines = [
        "Cost and usage accounting (MEMO-22)",
        "",
        _row("memos", f"{memos:,}"),
        _row(
            "transcribed",
            f"{spend['transcribed']:,} ({spend['recordings']:,} from a recording)",
        ),
        _row("enriched", f"{spend['enrichments']:,}"),
        _row(
            "audio",
            f"{spend['audio_minutes']:,.1f} minutes"
            f"{_spread(spend['shortest_ms'], spend['longest_ms'])}",
        ),
        _row(
            "enrichment tokens",
            f"{spend['input_tokens']:,} in, {spend['output_tokens']:,} out",
        ),
        "",
        "Measured spend",
        "",
        # The headline, and the one line whose value is read from the table rather
        # than computed: `memos.cost_micro_usd` is what a provider reported being
        # paid, summed. Zero on every local run.
        _row("charged by a provider", rates.usd(spend["measured_micro_usd"])),
        "    every model in this stack runs in the ai-worker container, so nothing",
        "    was billed and no key was needed. This is a reading, not an assumption.",
        "",
        f"Projection onto hosted providers (rates checked {rates.RATES_CHECKED})",
        "",
        f"  transcription  {report.stt_model}",
        _row("rate", f"${audio_rate.usd_per_audio_minute:.4f} per audio-minute", indent=4),
        _row(
            f"{spend['audio_minutes']:,.1f} audio-minutes",
            rates.usd(spend["projected_stt_micro_usd"]),
            indent=4,
        ),
        f"  enrichment     {report.enrich_model}",
        _row(
            "rate",
            f"${token_rate.usd_per_million_input:.2f} in /"
            f" ${token_rate.usd_per_million_output:.2f} out, per Mtok",
            indent=4,
        ),
        _row(
            f"{spend['input_tokens'] + spend['output_tokens']:,} tokens",
            rates.usd(spend["projected_enrich_micro_usd"]),
            indent=4,
        ),
        "",
        _row(f"total for these {memos:,} memos", rates.usd(projected)),
        _row(f"per {report.per:,} memos", _scaled(projected, memos, report.per)),
    ]

    lines += ["", "Local inference, which is what this design actually costs", ""]
    lines += _latency(report)

    # Memory is the other half of that sentence and this report deliberately prints
    # no number for it, which is a correction rather than an omission. The figure it
    # can reach is *this* process's -- a bare `python -m memo_ai.costs`, about 35 MB
    # -- and printing that under a heading about what the design costs invites
    # exactly one misreading: that a worker holding two models costs 35 MB. RSS
    # belongs to a process, the workers are other containers, and there is no
    # channel from here to theirs. So this points at the place the real numbers are.
    lines += [
        "",
        _row("resident memory", "per worker, in the worker's own log"),
        "    both models load lazily, so the figure moves: an idle replica is 18 MB",
        "    and one holding whisper and the enricher is about 1,708 MB, of which",
        "    1,081 MB is the mmap-ed weights and is shared with the other replica.",
        "    `docker compose logs ai-worker | grep rss`",
    ]

    return "\n".join(lines) + "\n"


# How wide the label column is, measured from the left margin rather than from the
# indent -- so a line indented four spaces gets a label four characters narrower
# and its value still lands in the same column as everything above it. Sized for
# `total for these 1,000 memos` plus a gap, which is the longest label the report
# produces at a plausible number of memos.
_LABEL_WIDTH = 30


def _row(label: str, value: str, indent: int = 2) -> str:
    """
    One ``label   value`` line, with the values in a column a reader can scan.

    Every figure in this report is a value in that column, which is the only
    formatting a ``docker compose run`` log preserves.

    **A label wider than the column pushes its own value right rather than being
    truncated, and it keeps a space.** That space is the whole reason this is not
    an ``ljust`` at the call site: ``_LABEL_WIDTH`` is sized for a thousand memos,
    and the label grows with the count, so at a million ``ljust`` returns the
    label unchanged and the value is appended straight onto it --
    ``total for these 1,000,000 memos$2.0000``. One long line that does not line
    up is a cosmetic problem; a number welded to the end of a word is a figure
    nobody can read, in the one output this task exists to produce.
    """
    column = max(_LABEL_WIDTH - indent, len(label) + 1)

    return f"{' ' * indent}{label.ljust(column)}{value}"


def _latency(report: Report) -> list[str]:
    """The two latency blocks, or a line saying why they are empty."""
    transcription = report.transcription
    enrichment = report.enrichment
    lines: list[str] = []

    if transcription["memos"]:
        lines += [
            _row(
                "transcription",
                f"{transcription['memos']:,} recordings,"
                f" {transcription['audio_minutes']:,.1f} minutes of audio",
            ),
            _row(
                "median",
                f"{transcription['median_seconds_per_minute']:.1f}s of inference per"
                f" audio-minute ({transcription['median_realtime_factor']:.2f}x realtime)",
                indent=4,
            ),
            _row(
                "p95",
                f"{transcription['p95_seconds_per_minute']:.1f}s per audio-minute",
                indent=4,
            ),
            # The total, which is the one figure here that is about the machine
            # rather than about a memo: how much CPU this stack has actually spent
            # transcribing. It is what a rate per audio-minute is worth knowing in
            # order to project.
            _row("total", _duration(transcription["inference_seconds"]), indent=4),
        ]
    else:
        # Not an error and worth saying plainly. A stack run entirely on text memos
        # or on `STT_PROVIDER=fake` has nothing to time, and a blank where a number
        # should be reads as a broken report rather than as an empty one.
        lines.append(_row("transcription", "no timed transcriptions yet"))

    if enrichment["memos"]:
        lines += [
            _row("enrichment", f"{enrichment['memos']:,} memos"),
            _row(
                "median",
                f"{enrichment['median_seconds']:.1f}s{_throughput(enrichment)}",
                indent=4,
            ),
            _row("p95", f"{enrichment['p95_seconds']:.1f}s", indent=4),
            _row("total", _duration(enrichment["inference_seconds"]), indent=4),
        ]
    else:
        lines.append(_row("enrichment", "no timed enrichments yet"))

    return lines


def _throughput(enrichment: dict[str, Any]) -> str:
    """
    `` (212 tokens/s)``, or nothing when no run reported its tokens.

    Nothing rather than a zero, and this is the branch the SQL's FILTER makes
    reachable: with it, ``median_tokens_per_second`` is NULL for a set where no
    enricher reported usage, and ``f"{None:,.0f}"`` is a ``TypeError`` in the
    middle of printing the report.
    """
    if enrichment.get("median_tokens_per_second") is None:
        return ""

    return f" ({enrichment['median_tokens_per_second']:,.0f} tokens/s)"


def _duration(seconds: float | None) -> str:
    """
    ``3.6 hours of inference`` -- a total, in whatever unit keeps it readable.

    Seconds below a minute, minutes below an hour, hours above. A cumulative
    inference figure spans four orders of magnitude between a stack somebody has
    just started and one that has run for a week, and "13,056.0s" is not a number
    anybody converts in their head.
    """
    if seconds is None:
        return "not measured"

    if seconds < 60:
        return f"{seconds:,.1f}s of inference"

    if seconds < 3600:
        return f"{seconds / 60:,.1f} minutes of inference"

    return f"{seconds / 3600:,.1f} hours of inference"


def _spread(shortest_ms: int | None, longest_ms: int | None) -> str:
    """
    ``, 4s to 9m 12s`` -- how mixed the set actually is, or nothing.

    Appended to the audio line rather than given its own, because it only means
    something beside the total it qualifies.
    """
    if shortest_ms is None or longest_ms is None:
        return ""

    return f", {_clock(shortest_ms)} to {_clock(longest_ms)}"


def _clock(ms: int) -> str:
    seconds = round(ms / 1000)

    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


def _scaled(micro_usd: int, memos: int, per: int) -> str:
    """
    The projection restated per ``per`` memos, or a refusal to divide by zero.

    The denominator is *every* memo rather than only the transcribed ones, and
    that is deliberate: "per 1000 memos" is a question about running the app, and
    an app whose users type half their memos really does cost half as much in
    transcription. Dividing by the recordings alone would quote the price of a
    voice-only deployment nobody has.
    """
    if not memos:
        return "no memos yet"

    return rates.usd(micro_usd * per / memos)


def _one(connection, sql: str, params: dict[str, Any]) -> dict[str, Any]:
    """
    Run one aggregate and return its single row as a dict.

    ``dict_row`` rather than the tuple psycopg returns by default, so that
    :func:`render` names the columns it prints. Every statement here has ten-odd
    of them and positional access would make adding one to the middle of a SELECT
    a silent reshuffle of the output.
    """
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()

    # Unreachable: every statement here is an ungrouped aggregate, which returns
    # exactly one row even over an empty table. Asserted rather than assumed
    # because the alternative is a TypeError from subscripting None, three
    # functions away from the statement that caused it.
    if row is None:
        raise RuntimeError("An aggregate over `memos` returned no row.")

    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m memo_ai.costs",
        description=(
            "What this stack has spent (nothing), what a hosted one would have, "
            "and how long local inference actually takes."
        ),
    )
    parser.add_argument(
        "--stt-model",
        default=rates.DEFAULT_STT_MODEL,
        help=f"hosted transcription model to project onto (default: {rates.DEFAULT_STT_MODEL})",
    )
    parser.add_argument(
        "--enrich-model",
        default=rates.DEFAULT_ENRICH_MODEL,
        help=f"hosted enrichment model to project onto (default: {rates.DEFAULT_ENRICH_MODEL})",
    )
    parser.add_argument(
        "--per",
        type=_positive,
        default=1000,
        help="restate the projection per this many memos (default: 1000)",
    )
    parser.add_argument(
        "--rates",
        action="store_true",
        help="print the rate table and exit, without touching the database",
    )
    args = parser.parse_args(argv)

    if args.rates:
        print(_rate_table(), end="")

        return 0

    try:
        settings = Settings.from_env()
    except ConfigError as error:
        print(f"memo-costs: {error}", file=sys.stderr)

        return EXIT_MISUSED

    try:
        # `role` so this shows up in pg_stat_activity as what it is rather than as
        # a third worker replica -- see memo_ai/db.py.
        with db.connect(settings, role="costs") as connection:
            report = collect(connection, args.stt_model, args.enrich_model, args.per)
    except ValueError as error:
        # The unknown-model refusal from memo_ai/rates.py, which names what it does
        # have. Caught here rather than left to a traceback because it is the one
        # error a person running this by hand will actually hit.
        print(f"memo-costs: {error}", file=sys.stderr)

        return EXIT_MISUSED
    except psycopg.OperationalError as error:
        # The other one they will hit, and the commoner of the two: running this
        # against a stack that is not up. `OperationalError` only -- the worker
        # narrows to the same class for the reason given in its `_run`, and a
        # mistake in one of these statements is a `ProgrammingError` that should
        # arrive as a traceback rather than as a sentence about the database.
        #
        # One line, not the driver's paragraph. libpq's message for an unreachable
        # host runs to three lines and repeats the host twice.
        print(
            f"memo-costs: cannot reach the database ({_first_line(error)}). "
            f"Is the stack up? `docker compose up -d db`",
            file=sys.stderr,
        )

        return EXIT_UNAVAILABLE

    print(render(report), end="")

    return 0


def _positive(raw: str) -> int:
    """
    ``--per`` as a whole number above zero.

    Validated by argparse rather than tolerated downstream, because neither
    degenerate value fails loudly on its own: ``--per 0`` prints a projection of
    ``$0.0000``, which reads as "this is free", and ``--per -1000`` prints negative
    money. Both are worse than a refusal.
    """
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a whole number, got {raw!r}") from None

    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be greater than zero, got {value}")

    return value


def _first_line(error: Exception) -> str:
    return str(error).strip().splitlines()[0] if str(error).strip() else error.__class__.__name__


def _rate_table() -> str:
    """The whole rate table with its provenance, for ``--rates``."""
    lines = [
        f"Rates in memo_ai/rates.py, checked {rates.RATES_CHECKED}.",
        "List prices, no volume tiers, and nothing here has ever been charged to",
        "this project. Re-check the provider before repeating a number.",
        "",
        "Transcription, per minute of audio",
        "",
    ]

    for name, rate in sorted(rates.STT_RATES.items()):
        lines.append(f"  {name:<24} ${rate.usd_per_audio_minute:.4f}   {rate.source}")

    lines += ["", "Enrichment, per million tokens", ""]

    for name, token_rate in sorted(rates.ENRICH_RATES.items()):
        lines.append(
            f"  {name:<24} ${token_rate.usd_per_million_input:.2f} in /"
            f" ${token_rate.usd_per_million_output:.2f} out   {token_rate.source}"
        )

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
