"""
The rate table's arithmetic, which is the half of MEMO-22 that can be wrong quietly.

Nothing here checks that a rate is *correct* -- they are list prices copied from
providers' pricing pages and nothing in this repo can validate them. What these
tests hold is the unit. A projection that reports $0.00 for a real 20-second memo
looks like a working feature, and it is the failure this whole module exists to
prevent: the task specifying it says so twice.
"""

import pytest

from memo_ai import rates


def test_a_short_memo_costs_more_than_nothing_at_a_hosted_rate():
    # The measurement the task names: about 20 seconds of audio, at whisper-1's
    # rate, is roughly 0.2 cents. The point is that it is not zero -- in integer
    # cents this rounds away, and a SUM over 1000 of them would read 0 against a
    # true $2.00.
    micro = rates.stt_rate("whisper-1").micro_usd(20_000)

    assert micro == 2_000
    assert rates.usd(micro) == "$0.0020"


def test_a_thousand_short_memos_come_to_real_money():
    # The other end of the same arithmetic, and the number the task wants somebody
    # to be able to say on a call. 1000 memos of 20 seconds is 333 minutes.
    total = rates.stt_rate("whisper-1").micro_usd(20_000) * 1000

    assert rates.usd(total) == "$2.0000"


def test_billing_is_per_audio_minute_and_scales_with_length_alone():
    # A memo twice as long costs twice as much, and nothing about sample rate,
    # channel count or file size enters into it. That is why memo_ai/audio.py's
    # downsampling saves bandwidth and not money, and why `duration_ms` is the only
    # column a transcription projection needs -- which is fortunate, because
    # whisper-1 returns no usage fields at all.
    rate = rates.stt_rate("whisper-1")

    assert rate.micro_usd(120_000) == 2 * rate.micro_usd(60_000)
    assert rate.micro_usd(60_000) == round(rate.usd_per_audio_minute * 1_000_000)


def test_input_and_output_tokens_are_priced_apart():
    # Every hosted provider bills output several times higher than input. A single
    # blended rate would misprice this workload badly, because an enrichment prompt
    # is large and its answer is capped at 256 tokens.
    rate = rates.enrich_rate("gpt-4o-mini")

    assert rate.micro_usd(input_tokens=1_000_000, output_tokens=0) == 150_000
    assert rate.micro_usd(input_tokens=0, output_tokens=1_000_000) == 600_000
    assert rate.micro_usd(input_tokens=1_000_000, output_tokens=1_000_000) == 750_000


def test_one_realistic_enrichment_is_a_fraction_of_a_cent():
    # ~800 tokens of prompt and ~100 of answer, which is what the shipped prompt
    # plus a short memo actually produces. Three hundred-millionths of a dollar --
    # under a hundredth of a cent, and still a number rather than a zero.
    micro = rates.enrich_rate("gpt-4o-mini").micro_usd(input_tokens=812, output_tokens=97)

    assert micro == 180
    assert rates.usd(micro) == "$0.0002"


def test_the_local_rate_prices_the_shipped_configuration_at_nothing():
    # Zero rather than absent, so `--stt-model local` is a legal projection whose
    # answer is $0.00 out of the same arithmetic as every other row, rather than a
    # special case in the printing code.
    assert rates.stt_rate("local").micro_usd(600_000) == 0
    assert rates.enrich_rate("local").micro_usd(10_000, 10_000) == 0


def test_every_rate_carries_its_provenance():
    # A number with no source is one somebody has to re-derive before they dare
    # quote it, and `--rates` prints this column for exactly that reason.
    for rate in list(rates.STT_RATES.values()) + list(rates.ENRICH_RATES.values()):
        assert rate.source.strip()


def test_the_defaults_are_models_the_table_actually_has():
    # The two names memo_ai/costs.py falls back to when nobody passes a flag. A typo
    # in either would make the bare command fail rather than the flag it belongs to.
    assert rates.DEFAULT_STT_MODEL in rates.STT_RATES
    assert rates.DEFAULT_ENRICH_MODEL in rates.ENRICH_RATES


@pytest.mark.parametrize("lookup", [rates.stt_rate, rates.enrich_rate])
def test_an_unknown_model_names_the_ones_that_exist(lookup):
    # The one error a person running the report by hand will actually hit, so it
    # says what to do about it rather than only what went wrong.
    with pytest.raises(ValueError, match="Known models:"):
        lookup("gpt-9-omni")


def test_the_formatter_keeps_the_sub_cent_digits():
    # Four decimal places, which is two more than money has. At two, both of the
    # per-memo figures above print as $0.00 and the report says a hosted provider is
    # free -- which is the one sentence it must never produce.
    assert rates.usd(200) == "$0.0002"
    assert rates.usd(0) == "$0.0000"

    # And the same format at the other magnitude, because a column whose precision
    # changes with its value is one nobody can scan.
    assert rates.usd(3_500_000) == "$3.5000"
    assert rates.usd(12_345_678_900) == "$12,345.6789"
