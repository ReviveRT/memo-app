"""
Name to provider. The registry, and the only place that knows what names exist.

The set below has to agree with three other files that a reader may well trust
before this one: the ``STT_PROVIDER`` comment in .env.example, the variable table
in README.md, and the ``STT_PROVIDER``/``STT_FALLBACK`` defaults in
docker-compose.yml. A name that is documented there and unknown here would be
rejected at boot with "unknown provider" while the repo's own documentation
recommends it -- which is why `local` and `openai` are registered as
:class:`~memo_ai.stt.unimplemented.UnimplementedStt` rather than left out.
"""

from memo_ai.config import ConfigError, Settings
from memo_ai.stt.base import SttError, SttProvider, SttUnavailable, Transcript
from memo_ai.stt.fake import FakeStt
from memo_ai.stt.unimplemented import UnimplementedStt

__all__ = [
    "PROVIDER_NAMES",
    "SttError",
    "SttProvider",
    "SttUnavailable",
    "Transcript",
    "require_known",
    "resolve",
]

# Which task owes each name an implementation. Keyed rather than listed so that
# the message a user sees names the task, and so that deleting an entry from here
# is what turns a name from "not yet" into "not a thing".
_UNIMPLEMENTED = {
    "local": "MEMO-14",
    "openai": "MEMO-14, optional",
}

PROVIDER_NAMES = frozenset({FakeStt.name, *_UNIMPLEMENTED})


def resolve(name: str, settings: Settings) -> SttProvider:
    """
    Build the provider for ``name``, or refuse to start.

    ``settings`` is unused by every provider that exists today -- the fake reads no
    configuration and the unimplemented ones read none either. It is in the
    signature anyway because MEMO-14's local provider needs ``stt_model``, and the
    alternative is that adding it changes every call site and every test double at
    the same time as introducing the thing being tested.
    """
    if name == FakeStt.name:
        return FakeStt()

    if name in _UNIMPLEMENTED:
        return UnimplementedStt(name, owner=_UNIMPLEMENTED[name])

    raise _unknown("STT_PROVIDER", name)


def require_known(variable: str, name: str) -> None:
    """
    Validate a provider name without building it.

    Exists for ``STT_FALLBACK``, which MEMO-08 does not use: the fallback *chain*
    is MEMO-14's, along with the classification that decides when to walk it. What
    MEMO-08 can do cheaply is refuse to start on a typo in it, so that
    ``STT_FALLBACK=fak`` is caught on the boot after the edit rather than months
    later, on the one code path that only runs when something else has already
    gone wrong.
    """
    if name not in PROVIDER_NAMES:
        raise _unknown(variable, name)


def _unknown(variable: str, name: str) -> ConfigError:
    allowed = ", ".join(sorted(PROVIDER_NAMES))

    return ConfigError(f"{variable} must be one of {allowed}, got {name!r}.")
