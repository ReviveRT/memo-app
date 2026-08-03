"""
What ``ask/app.py`` and ``ask/service.py`` need from whatever answers the question.

Three members, and the file exists because there are now two implementations of them
-- :class:`~memo_ai.ask.model.Model` running llama.cpp in this container, and
:class:`~memo_ai.ask.hosted.HostedModel` sending the same prompt to Groq. Before the
second one the annotation could simply say ``Model`` and be both a type and the truth;
with two it has to say what is actually required, or the type is a lie that happens to
run.

**A Protocol rather than a base class**, and that is the deliberate choice of the two.
Neither implementation should have to import the other or a shared parent to be
usable: ``Model`` predates this file and is not modified by it, and a test double --
``tests/test_ask_app.py`` has several -- satisfies this by having the three members
rather than by inheriting anything. Structural typing is what the existing tests were
already relying on informally; this writes it down.

``runtime_checkable`` is deliberately *not* applied. It would only enable
``isinstance``, and an ``isinstance`` check against this protocol is exactly the
branch neither module should contain -- ``service.py`` asking which backend it has is
the coupling this seam exists to prevent. The factory in ``memo_ai/ask/__main__.py``
is the one place that knows, and it decides once, from configuration.
"""

from collections.abc import Iterator
from typing import Protocol


class Backend(Protocol):
    """A thing that can answer a question, streaming."""

    def start_loading(self) -> None:
        """
        Begin any warm-up, without blocking. Called once from the startup hook.

        A no-op is a valid implementation and the hosted backend's is one: there is
        nothing to load. It stays in the protocol because the caller cannot know that
        and must not have to ask.
        """

    @property
    def state(self) -> str:
        """
        ``ready``, or a key of :data:`~memo_ai.ask.model.UNAVAILABLE` saying why not.

        A property rather than a method, matching ``Model``, because ``/health`` and
        ``/ask`` both read it as an attribute and changing that would be a change to
        two routes for no gain. Which keys can appear depends on the implementation
        and no caller should care -- both of them look the answer up in the one
        mapping rather than switching on it.
        """

    def stream(self, messages: list[dict[str, str]]) -> Iterator[str]:
        """
        Yield the answer in chunks, or raise
        :class:`~memo_ai.ask.model.ModelUnavailable`.

        ``messages`` is OpenAI-shaped -- ``{"role", "content"}`` -- which is what
        ``memo_ai/ask/prompt.py`` already produced for llama.cpp's chat wrapper and,
        by luck rather than design, exactly what Groq's endpoint accepts. That
        coincidence is why this protocol has three members instead of a translation
        layer.
        """
