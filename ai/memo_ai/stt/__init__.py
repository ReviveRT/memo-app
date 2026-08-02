"""
Name to provider. The registry, and the only place that knows what names exist.

The set below has to agree with three other files that a reader may well trust
before this one: the ``STT_PROVIDER`` comment in .env.example, the variable table
in README.md, and the ``STT_PROVIDER``/``STT_FALLBACK`` defaults in
docker-compose.yml. A name that is documented there and unknown here would be
rejected at boot with "unknown provider" while the repo's own documentation
recommends it -- which is why `openai` is registered as
:class:`~memo_ai.stt.unimplemented.UnimplementedStt` rather than left out.

Three of the four names are real, which is the point of the seam rather than a
milestone: `local`, `fake` and `groq` are all implemented, all tested and all
exercised by the same pipeline, so the interface is proven by use instead of
asserted by having only one shape pass through it. `openai` is the one left
undone, on purpose -- see unimplemented.py.

**`groq` is the hosted provider that seam was always for**, and it arrives without
displacing anything: `local` remains the default in docker-compose.yml, in
.env.example and here, because it is the one that needs no key and no network, and
a clean `docker compose up` must keep working with neither. What `groq` adds is a
faster option for somebody who has a key and has read what leaving the machine
means -- memo_ai/stt/groq.py states it, and README.md states it where a person
choosing a provider will see it.
"""

from memo_ai.config import ConfigError, Settings
from memo_ai.stt.base import SttError, SttProvider, SttUnavailable, Transcript
from memo_ai.stt.chain import FallbackStt
from memo_ai.stt.fake import FakeStt
from memo_ai.stt.groq import GroqStt
from memo_ai.stt.local import LocalWhisperStt
from memo_ai.stt.unimplemented import UnimplementedStt

__all__ = [
    "PROVIDER_NAMES",
    "FallbackStt",
    "GroqStt",
    "SttError",
    "SttProvider",
    "SttUnavailable",
    "Transcript",
    "require_known",
    "resolve",
    "resolve_chain",
]

# Which names are recognised without being built, and the clause that explains
# each. Keyed rather than listed so the message a user sees says why the name
# exists at all, and so that deleting an entry from here is what turns a name from
# "declined" into "not a thing".
#
# Short, because it is a clause in a sentence that can reach `memos.last_error`
# and from there the browser. The full reasoning belongs in the README, where
# somebody choosing a provider will read it; a failed memo wants the way out.
_UNIMPLEMENTED = {
    "openai": "the hosted adapter was deliberately left unwritten",
}

PROVIDER_NAMES = frozenset({FakeStt.name, GroqStt.name, LocalWhisperStt.name, *_UNIMPLEMENTED})


def resolve(name: str, settings: Settings) -> SttProvider:
    """
    Build the provider for ``name``, or refuse to start.

    Nothing here is expensive. :class:`LocalWhisperStt` loads no model until the
    first voice memo, which is what lets the committed default resolve at boot on
    a machine that has never downloaded a weight -- see that class for why a boot
    failure is the one outcome the default configuration may not have.
    """
    if name == FakeStt.name:
        return FakeStt()

    if name == LocalWhisperStt.name:
        return LocalWhisperStt(settings.stt_model, settings.stt_language)

    if name == GroqStt.name:
        # Built with whatever `GROQ_API_KEY` held, including nothing. A missing key
        # is a failed voice memo with a sentence naming the variable, never a failed
        # boot -- the rule UnimplementedStt set, and the one that keeps
        # `restart: unless-stopped` from turning a forgotten key into a loop that
        # also stops text memos. memo_ai/stt/groq.py has it at the class.
        return GroqStt(settings.groq_api_key, settings.groq_stt_model)

    if name in _UNIMPLEMENTED:
        return UnimplementedStt(name, because=_UNIMPLEMENTED[name])

    raise _unknown("STT_PROVIDER", name)


def resolve_chain(settings: Settings) -> SttProvider:
    """
    The configured provider, wrapped in its fallback if the two differ.

    Collapsing the equal case matters rather than being tidy, because equal is the
    *default*: docker-compose.yml ships ``STT_PROVIDER=local`` and
    ``STT_FALLBACK=local``. A chain built from one provider twice would call it
    again on every ``SttUnavailable`` -- so a model that failed to load would fail
    to load twice per memo, and each attempt would wait out its own
    ``MODEL_LOAD_TIMEOUT_SECONDS``, doubling the time a stuck download holds a
    replica in exchange for nothing.

    ``require_known`` still runs on the fallback even in the collapsed case, so
    ``STT_FALLBACK=fak`` is a refusal to boot rather than a surprise on the one
    code path that only runs when something else has already gone wrong. It is
    also what puts the *right* variable in the message: reaching ``resolve`` with
    a fallback name would report the typo against ``STT_PROVIDER``, and sending
    someone to check the wrong line of their .env is worse than saying nothing.
    """
    primary = resolve(settings.stt_provider, settings)
    require_known("STT_FALLBACK", settings.stt_fallback)

    if settings.stt_fallback == settings.stt_provider:
        return primary

    return FallbackStt(primary, resolve(settings.stt_fallback, settings))


def require_known(variable: str, name: str) -> None:
    """Validate a provider name without building it."""
    if name not in PROVIDER_NAMES:
        raise _unknown(variable, name)


def _unknown(variable: str, name: str) -> ConfigError:
    allowed = ", ".join(sorted(PROVIDER_NAMES))

    return ConfigError(f"{variable} must be one of {allowed}, got {name!r}.")
