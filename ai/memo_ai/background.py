"""
One callable on a daemon thread, with the outcome readable from outside.

Extracted from memo_ai/stt/local.py, where it lived while two things needed it,
because MEMO-21 made it three: loading the whisper model, draining its segment
generator, and now loading and running the local language model. All three need
it for the same reason, and it is the reason worth carrying with the class --
**the work is C++ that cannot be interrupted**, so the only way to bound it is to
stop *waiting* for it and let it finish unattended.

That is a weaker guarantee than a timeout usually implies and the difference
matters at both call sites. The work does not stop; the caller stops watching. A
model load that was going to take four minutes still takes four minutes, and the
memo that gave up on it at two has failed while the thread carries on -- which is
exactly what is wanted, because the *next* memo then finds the model loaded
rather than starting a second load of the same weights.
"""

import threading
from collections.abc import Callable


class BackgroundCall:
    """
    Run ``work`` on a daemon thread now; read ``result`` or ``error`` later.

    A bare ``threading.Thread`` rather than ``concurrent.futures``, and the reason
    is shutdown. ``ThreadPoolExecutor`` registers an ``atexit`` hook that joins its
    workers, so a call this class has already given up waiting for would block the
    interpreter from exiting -- which is precisely the hang the timeouts exist to
    prevent, moved from one memo to ``docker compose down``. A daemon thread is
    abandoned at exit instead.
    """

    def __init__(self, work: Callable[[], object], name: str = "background") -> None:
        self.result: object | None = None
        self.error: BaseException | None = None
        self._done = threading.Event()

        threading.Thread(target=self._run, args=(work,), name=name, daemon=True).start()

    def _run(self, work: Callable[[], object]) -> None:
        try:
            self.result = work()
        except BaseException as error:  # noqa: BLE001 -- reported to the waiter, not swallowed
            self.error = error
        finally:
            # In the finally, so work that raises still releases whoever is waiting
            # on it. Without this a failed call reads exactly like a slow one, for
            # the full timeout, every time.
            self._done.set()

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    @property
    def done(self) -> bool:
        """
        Finished, either way. Asked by a caller deciding whether it may start
        another one -- see ``LocalLlmEnricher._generate``, where the work is a
        llama.cpp context that two threads may not enter at once.
        """
        return self._done.is_set()

    @property
    def failed(self) -> bool:
        """
        Finished, and finished badly. Both halves matter.

        A call still in flight is not failed, which is what stops a memo that timed
        out waiting on a model load from causing the next one to start a second
        load of the same weights.
        """
        return self._done.is_set() and self.error is not None
