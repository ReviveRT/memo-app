"""
What hosted providers charge, and the arithmetic for turning usage into money.

**Nothing in this project has ever been billed.** Every provider that runs here
runs on this machine: faster-whisper in the worker process, llama.cpp in the same
one, and both from weights baked into the image. ``memos.cost_micro_usd`` is 0 or
NULL on every row, and it is *supposed* to be.

This module exists so that being unbilled does not also mean being unable to
answer "what would this cost per 1000 memos?". The inputs to a hosted invoice --
minutes of audio, tokens in, tokens out -- are measurable for free, and the worker
records them (db/migrations/006_cost_accounting.sql). What is left is a rate to
multiply them by, which is the table below. memo_ai/costs.py is the one reader.

**The unit everywhere is the micro-dollar**, a millionth of a dollar, matching
``memos.cost_micro_usd``. That is not over-engineering for a project that spends
nothing -- it is what stops the answer being *wrong* the day somebody points
``STT_PROVIDER`` at a hosted provider. A 20-second memo transcribed at
``whisper-1``'s rate costs 0.2 cents; in integer cents that is 0, and a SUM over
1000 such memos reads 0 against a true $2.00. The correct unit costs nothing to
keep and the wrong one fails silently on exactly the rows there are most of.

**These numbers are inputs to a projection, not a quote.** They are list prices
noted on the date below, they exclude volume discounts, batch tiers and currency,
and published rates move. Every figure memo_ai/costs.py prints names the rate it
used and the date on it, so nothing it says can be mistaken for an invoice; before
repeating a number to anybody who cares about it, re-check the provider's own
pricing page and edit this file. Nothing here is read at boot and nothing depends
on it being current -- a stale rate produces a wrong projection and no other
effect anywhere in the stack.
"""

from collections.abc import Mapping
from dataclasses import dataclass

# When the rates below were last checked against the providers' published pricing.
#
# Printed beside every projection rather than kept as a comment, because the one
# thing a reader of a dollar figure needs to know about it is how old the rate
# behind it is. A date in the output invites the question; a date in a source file
# does not.
RATES_CHECKED = "2026-08-02"

# A dollar, in micro-dollars. Named rather than written as 1_000_000 at each of
# the four sites that need it, so a reader who has understood the unit once does
# not have to count zeroes again.
MICRO_USD_PER_USD = 1_000_000

MS_PER_MINUTE = 60_000

# Rates are quoted per million tokens by every provider that has them, so the
# table below stores them that way and the conversion happens here.
TOKENS_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class AudioRate:
    """
    What one model charges per minute of audio.

    **Per minute, and that is the whole shape of hosted transcription billing.**
    Not per byte, not per request, and not per sample -- which is worth stating
    because it is the one place an intuition about this pipeline is actively
    wrong. memo_ai/audio.py downsamples every upload to 16 kHz mono before
    transcription, and that saves bandwidth and disk and buys exactly nothing on
    a bill. The levers that move this number are the model and
    ``MAX_AUDIO_SECONDS``.

    It also means ``duration_ms`` is the only input a projection needs, which is
    fortunate: ``whisper-1`` returns no usage fields at all, so on that model it
    is the only input a projection could *have*.
    """

    usd_per_audio_minute: float

    # Where the number came from, printed in `--rates`. A rate with no provenance
    # is a number somebody has to re-derive before they dare quote it.
    source: str

    def micro_usd_per_minute(self) -> float:
        return self.usd_per_audio_minute * MICRO_USD_PER_USD

    def micro_usd(self, duration_ms: int) -> int:
        """What one recording of this length would cost, rounded to the micro-dollar."""
        return round(duration_ms / MS_PER_MINUTE * self.micro_usd_per_minute())


@dataclass(frozen=True)
class TokenRate:
    """
    What one model charges per token, in and out.

    Two rates rather than one, because every hosted provider bills output several
    times higher than input -- four to five times, on the models below. A single
    blended rate would misprice this workload badly in the cheap direction: an
    enrichment prompt is ~250 tokens of instructions, ~250 of worked examples and
    the whole memo, against an answer capped at 256 tokens by
    ``MAX_OUTPUT_TOKENS``, so the input side dominates the count and the output
    side dominates the price per token. That is also why
    db/migrations/006_cost_accounting.sql keeps the two counts in separate
    columns.
    """

    usd_per_million_input: float
    usd_per_million_output: float
    source: str

    def micro_usd_per_input_token(self) -> float:
        return self.usd_per_million_input * MICRO_USD_PER_USD / TOKENS_PER_MILLION

    def micro_usd_per_output_token(self) -> float:
        return self.usd_per_million_output * MICRO_USD_PER_USD / TOKENS_PER_MILLION

    def micro_usd(self, input_tokens: int, output_tokens: int) -> int:
        """What one enrichment of this size would cost, rounded to the micro-dollar."""
        return round(
            input_tokens * self.micro_usd_per_input_token()
            + output_tokens * self.micro_usd_per_output_token()
        )


# What runs here, priced honestly.
#
# Zero rather than absent, and this entry earns its place: it is what makes
# `--stt-model local` a legal projection whose answer is $0.00, so the report can
# state the shipped configuration's cost as a number from the same arithmetic as
# every other row rather than as a special case in the printing code.
#
# It is not the same statement as `memos.cost_micro_usd IS NULL`, which is what
# the rows actually carry. NULL there means "no provider reported a charge"; zero
# here means "this rate table prices this model at nothing". The report shows both
# and they agree, which is the check worth having.
LOCAL_AUDIO_RATE = AudioRate(
    usd_per_audio_minute=0.0,
    source="faster-whisper in this container -- no account, no network, no bill",
)

LOCAL_TOKEN_RATE = TokenRate(
    usd_per_million_input=0.0,
    usd_per_million_output=0.0,
    source="llama.cpp in this process -- no account, no network, no bill",
)

# Per minute of audio. The keys are the model names a provider would accept, so
# that `--stt-model whisper-1` reads the same as the configuration it is
# projecting.
#
# `local` is the model this stack actually runs; the rest are what
# `STT_PROVIDER=openai` would have cost had the adapter been written. It was
# deliberately not written -- memo_ai/stt/unimplemented.py has that argument --
# and this table is what lets the decision be priced without reversing it.
STT_RATES: dict[str, AudioRate] = {
    "local": LOCAL_AUDIO_RATE,
    # The one entry here that is not hypothetical: `STT_PROVIDER=groq` is
    # implemented (memo_ai/stt/groq.py), so a projection against this rate is
    # pricing a path somebody can actually run today.
    #
    # $0.04 per audio-hour, which is $0.000667 a minute -- an order of magnitude
    # under whisper-1 for the same weights. **Billed at a 10-second minimum per
    # request**, which this rate does not model and which matters at this app's
    # shape: a 4-second memo is charged as 10, so a corpus of very short memos
    # costs more than `sum(duration_ms)` implies. The projection is therefore a
    # floor rather than an estimate for short recordings, and the report says as
    # much rather than silently rounding.
    #
    # Free tier at the checked date: 2,000 requests and 28,800 audio-seconds a day,
    # 20 requests a minute -- eight hours of audio daily at no charge, which is
    # more than this app generates. That is a $0.00 bill, not a $0.00 rate, so it
    # is a README sentence rather than an entry here.
    "groq-whisper-large-v3-turbo": AudioRate(
        usd_per_audio_minute=0.04 / 60,
        source=f"Groq list price, $0.04/audio-hour, 10s minimum, checked {RATES_CHECKED}",
    ),
    "groq-whisper-large-v3": AudioRate(
        usd_per_audio_minute=0.111 / 60,
        source=f"Groq list price, $0.111/audio-hour, 10s minimum, checked {RATES_CHECKED}",
    ),
    "whisper-1": AudioRate(
        usd_per_audio_minute=0.006,
        source=f"OpenAI list price, ${0.006:.3f}/minute, checked {RATES_CHECKED}",
    ),
    "gpt-4o-transcribe": AudioRate(
        usd_per_audio_minute=0.006,
        source=f"OpenAI list price, ${0.006:.3f}/minute, checked {RATES_CHECKED}",
    ),
    "gpt-4o-mini-transcribe": AudioRate(
        usd_per_audio_minute=0.003,
        source=f"OpenAI list price, ${0.003:.3f}/minute, checked {RATES_CHECKED}",
    ),
}

# Per million tokens, input and output.
#
# Three hosted models spanning roughly a factor of twenty, which is the useful
# range for the question this table answers: the projection is only interesting if
# it shows what the model choice is worth, and one hosted rate would not.
ENRICH_RATES: dict[str, TokenRate] = {
    "local": LOCAL_TOKEN_RATE,
    "gpt-4o-mini": TokenRate(
        usd_per_million_input=0.15,
        usd_per_million_output=0.60,
        source=f"OpenAI list price, $0.15/$0.60 per Mtok, checked {RATES_CHECKED}",
    ),
    "claude-haiku-4-5": TokenRate(
        usd_per_million_input=1.00,
        usd_per_million_output=5.00,
        source=f"Anthropic list price, $1/$5 per Mtok, checked {RATES_CHECKED}",
    ),
    "claude-sonnet-4-5": TokenRate(
        usd_per_million_input=3.00,
        usd_per_million_output=15.00,
        source=f"Anthropic list price, $3/$15 per Mtok, checked {RATES_CHECKED}",
    ),
}

# What `python -m memo_ai.costs` projects onto when nobody says otherwise.
#
# `whisper-1` because it is the model `STT_PROVIDER=openai` would most plausibly
# have reached for, and because it is the one whose billing shape this project
# already had to understand: it returns no usage fields, so duration is not merely
# the easiest input to a projection but the only one there is.
#
# `gpt-4o-mini` because enrichment here is a labelling job that a 1.5B model does
# adequately, so the honest hosted comparison is the cheapest capable model rather
# than the best one. Projecting onto a frontier model would inflate the answer by
# twenty times and describe a system nobody would have built.
DEFAULT_STT_MODEL = "whisper-1"
DEFAULT_ENRICH_MODEL = "gpt-4o-mini"


def stt_rate(model: str) -> AudioRate:
    """The audio rate for ``model``, or raise naming what is on offer."""
    if model not in STT_RATES:
        raise _unknown(STT_RATES, model, "transcription")

    return STT_RATES[model]


def enrich_rate(model: str) -> TokenRate:
    """The token rate for ``model``, or raise naming what is on offer."""
    if model not in ENRICH_RATES:
        raise _unknown(ENRICH_RATES, model, "enrichment")

    return ENRICH_RATES[model]


def usd(micro_usd: float) -> str:
    """
    Micro-dollars as a string a person reads, keeping the sub-cent digits.

    Four decimal places, which is two more than money has, and that is the whole
    reason this function exists rather than an f-string at each call site. One
    memo's projected transcription is $0.0020 and one memo's projected enrichment
    is $0.0003; at two places both print as $0.00 and the report says the hosted
    provider is free. The totals in the same output run to whole dollars, so the
    two are formatted the same way rather than switched between -- a column where
    the precision changes with the magnitude is a column nobody can scan.
    """
    return f"${micro_usd / MICRO_USD_PER_USD:,.4f}"


def _unknown(table: Mapping[str, object], model: str, what: str) -> ValueError:
    """
    The refusal, built in one place so both lookups word it the same way.

    A plain ``ValueError`` rather than :class:`~memo_ai.config.ConfigError`: this
    is an argument to a report somebody is running by hand, not a variable in a
    ``.env``, and that exception is the one the worker exits 2 on.
    """
    known = ", ".join(sorted(table))

    return ValueError(
        f"No {what} rate for {model!r}. Known models: {known}. "
        f"Add it to memo_ai/rates.py -- nothing validates these against a provider."
    )
