"""
The queue consumer. Run as ``python -m memo_ai.worker``.

Two replicas of this run under compose (``deploy: replicas: 2``), because one
serial worker means a text memo queues behind up to ``MAX_AUDIO_SECONDS`` of audio
and the fast path the UI advertises would not exist. Everything that makes two
consumers safe is in memo_ai/memos.py: ``FOR UPDATE SKIP LOCKED`` on the claim and
a ``locked_at`` fence on every result write.
"""
