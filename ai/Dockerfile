# syntax=docker/dockerfile:1

# The image behind two services. `ai-worker` is this file's CMD (MEMO-08) and
# `ai-api` (MEMO-24, a stretch) is a `command:` override on the same image, which
# is what "same image as ai-api, different entrypoint" means here -- one build
# context, one dependency set, one uid.
#
# `python:3.12-slim`, and both halves of that are chosen rather than default.
# 3.12 because compose already documents this image as it; slim rather than alpine
# because psycopg-binary publishes manylinux wheels and no musl ones, so alpine
# would build libpq from source and need a toolchain this image otherwise has no
# use for. See ai/requirements.txt.
#
# There is no `apt-get` in this file. Nothing here needs a system package yet --
# ffmpeg and ffprobe arrive with MEMO-13, and that is the task that owns the apt
# layer and the size it adds.
FROM python:3.12-slim

# PYTHONUNBUFFERED because stdout to a pipe is block-buffered, so the last log
# lines before a hard exit are lost exactly when they are worth reading. It is what
# lets memo_ai/log.py keep to plain `logging` with no flushing of its own.
#
# PYTHONDONTWRITEBYTECODE because /app is owned by root and the process runs as
# `memo` (below), so every import would attempt a .pyc write, fail, and silently
# carry on. Turning it off makes that a decision instead of a swallowed EACCES.
#
# PIP_NO_CACHE_DIR as an ENV rather than a --no-cache-dir flag, so it also covers
# the pip run that installs requirements-dev.txt when the tests are run in a
# container from this image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies before application code, so editing the claim loop does not
# re-resolve and re-download the driver.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# --- The shared-volume uid/gid contract (MEMO-12) ----------------------------
#
# The `audio` volume is written by the api container and read *and unlinked* by
# this one. unlink(2) checks write permission on the containing directory rather
# than on the file, so two containers that disagree about uid/gid produce a worker
# that can read every blob and delete none of them -- silently, with the memo
# transcribing fine and the volume filling up. NOTES.md has the reproduction.
#
# The decision was made in MEMO-12 and this file is the second half of it: uid
# 10001, gid 10001, named `memo`. The numbers are repeated from the `x-audio-user`
# anchor in docker-compose.yml deliberately rather than derived from it, because
# this image also has to be right under a bare `docker run` with no compose file
# to supply a `user:`. Changing them means editing both files.
#
# Not `--system`: Debian's SYS_UID_MAX is 999, so `useradd -r -u 10001` creates the
# account correctly and then warns about it on every build.
ARG APP_UID=10001
ARG APP_GID=10001

# Where the model weights cache lands. Compose mounts the `whisper-cache` volume
# here and points HF_HOME at /cache/huggingface.
ARG CACHE_DIR=/cache

# Two directories, and the reason for each is a different failure:
#
#  1. ${CACHE_DIR} has to exist *in the image* and be owned by `memo`, because
#     Docker seeds an empty named volume from its mount point -- uid, gid and mode
#     copied out of the image. Without this line `whisper-cache` comes up
#     root:root and the non-root worker cannot write a model into it, which
#     surfaces in MEMO-15 as a download that fails on every boot.
#
#     Only /cache, not /cache/huggingface. Docker copies the mount point's
#     *contents* into an empty volume too, and there is no reason to pre-populate
#     it with an empty directory the library creates for itself.
#
#  2. /home/memo, because the base image points HOME at /root, which uid 10001
#     cannot read. Nothing in the worker writes to HOME today; a python library
#     that decides to cache something there is the ordinary way that changes, and
#     the failure would be an unexplained EACCES from inside a dependency.
#
# And one directory this image must NOT create: ${AUDIO_DIR}. An image with no
# such path leaves an already-seeded volume alone, which is what lets the api image
# alone own the mode. One that created it under any owner would win the seed
# whenever it started first -- and `api` and `ai-worker` start concurrently, both
# gated on the same two conditions. Verified in both orders in MEMO-12. The mount
# point still appears at runtime because compose mounts the volume over it.
RUN groupadd --gid ${APP_GID} memo \
    && useradd --uid ${APP_UID} --gid ${APP_GID} --home-dir /home/memo \
        --shell /usr/sbin/nologin memo \
    && mkdir -p ${CACHE_DIR} /home/memo \
    && chown ${APP_UID}:${APP_GID} ${CACHE_DIR} /home/memo

ENV HOME=/home/memo

# Everything above ran as root on purpose: /app and the installed packages stay
# root-owned and read-only to the running worker.
USER memo

# Exec form, so python is PID 1 and receives SIGTERM directly. The shell form would
# put /bin/sh at PID 1, which does not forward signals to its child -- and
# `docker compose down` would then wait out the full stop grace period and SIGKILL
# a worker mid-job. memo_ai/worker/__main__.py installs the handler that makes the
# other half of that work.
#
# `-m memo_ai.worker` rather than a script path: it is the module path an `ai-api`
# service overrides with its own `command:`, and it keeps /app on sys.path without
# a PYTHONPATH or an install step.
CMD ["python", "-m", "memo_ai.worker"]
