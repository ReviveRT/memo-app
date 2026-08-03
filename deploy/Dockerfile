# The production image: one container serving the built SPA, the API and a transcription
# worker from one origin.
#
# **Build this from the repository root**, not from api/ or web/ -- it needs api/, web/, db/
# and ai/memo_ai/ together:
#
#     docker build -f deploy/Dockerfile -t memo-app .
#
# This is a separate file from api/Dockerfile rather than a replacement for it, and the
# difference is not duplication for its own sake. api/Dockerfile builds the *development*
# container, which is mounted over with a bind mount and sits behind Vite's dev server so the
# frontend has hot reload. Baking the SPA into that image would destroy the thing it exists
# for. So: compose keeps two containers and a dev server, hosting gets one container and a
# built bundle, and the only thing they must agree about is the PHP configuration -- which is
# why conf.d/ is copied from the same place in both.
#
# Why one container at all: the owner cookie needs one origin (deploy/Caddyfile has the
# argument), and free tiers give you one web service. The two constraints point the same way.
#
# Not included: the 2.8 GB of model weights ai/Dockerfile bakes, and `ai-api` with them --
# neither fits a free tier's memory. What *is* included is the worker's source, running against
# a hosted provider; the worker section further down has that argument and its cost.
#
# The claim this header used to make -- that the application "degrades honestly" without any
# worker, a voice memo staying `queued` while "a text memo works completely" -- was measured
# and is false. A typed memo is written `queued` too and only the worker moves it on, so an
# API-only image left every card on the page waiting forever. That is why the worker is here.
#
# The one feature genuinely absent is Ask, which needs `ai-api` and a local LLM. /api/ask
# answers 503 and web/src/api/ask.js turns that into a sentence for the user rather than
# showing them the API's developer-facing text.

# --------------------------------------------------------------------------
# The SPA
# --------------------------------------------------------------------------

FROM node:24-alpine AS web

WORKDIR /web

# package.json and the lockfile first, so `npm ci` is only re-run when dependencies actually
# change rather than on every source edit.
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build

# --------------------------------------------------------------------------
# PHP dependencies
# --------------------------------------------------------------------------

FROM composer:2 AS vendor

WORKDIR /api

COPY api/composer.json api/composer.lock* ./

# --no-dev, matching api/Dockerfile: the shipped image carries no test framework. One
# consequence worth knowing before debugging a deployment -- `php artisan test` does not exist
# in this image, and neither does phpunit.
RUN composer install \
        --no-dev \
        --no-interaction \
        --no-progress \
        --prefer-dist \
        --no-scripts \
        --no-autoloader \
    && composer clear-cache

# --------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------

FROM dunglas/frankenphp:1-php8.3

# --- The file capability that stops this image booting on a hosted platform ----
#
# The base image ends with `setcap cap_net_bind_service=+ep /usr/local/bin/frankenphp`, so it
# can bind :80 as a non-root user. This image never binds a privileged port -- :8080 by
# default and :10000 on Render, both above 1024 -- so the capability buys nothing here, and
# the `+e` bit on it actively breaks the container.
#
# The mechanism, because the symptom names none of it: when the kernel execs a file whose
# *effective* capability bit is set, it requires those capabilities to be inside the process's
# capability bounding set. Container runtimes are free to narrow that set, and hosted ones do
# -- they have no reason to grant NET_BIND_SERVICE to a service they hand a high port. The
# exec then fails with EPERM, which surfaces as:
#
#     /usr/local/bin/memo-entrypoint: line 56: /usr/local/bin/frankenphp: Operation not permitted
#     ==> Exited with status 126
#
# and says nothing whatsoever about capabilities.
#
# This is why the image passed every local test and failed its first real deploy. Docker's
# default capability set *includes* NET_BIND_SERVICE, so the bounding set locally is wide
# enough and the exec succeeds; Render's is not. Reproduced on this machine, which is the only
# reason the cause is stated rather than guessed:
#
#     docker run --rm --cap-drop NET_BIND_SERVICE <image>   # EPERM, identical to Render
#     docker run --rm <image>                               # FrankenPHP starts
#
# `setcap -r` removes the attribute entirely, which makes the binary an ordinary executable
# whose exec depends on nothing the runtime might have dropped. Not `--cap-add` on the
# platform side: that would be a setting every future deployment has to remember, on a
# capability this image has no use for.
#
# Deliberately not applied to api/Dockerfile. That image runs under local Docker, where the
# capability is present and harmless, and narrowing the diff keeps this fix to the file whose
# deployment target actually drops it.
# The `test -z` is an assertion, not decoration: if a future base image sets the capability by
# some other means, or setcap silently no-ops, the build fails here rather than producing an
# image that dies on its first deploy with an error naming a shell line number.
RUN setcap -r /usr/local/bin/frankenphp \
    && test -z "$(getcap /usr/local/bin/frankenphp)"

# Three additions over api/Dockerfile, and they are three separate arguments.
#
# postgresql-client is for migrations. Compose runs those as a separate one-shot `migrate`
# service; a single container has nowhere to put one, so deploy/entrypoint.sh runs
# db/migrate.sh at boot and that script is psql.
#
# python3 is for the worker that runs beside the API in this image -- see the worker section
# below for why it is here rather than in a service of its own. python3-venv because Debian
# marks its interpreter externally-managed (PEP 668) and a bare `pip install` refuses; a venv
# is the sanctioned answer and `--break-system-packages` is the one that earns its name.
#
# ffmpeg is the worker's, and it is needed on *every* path rather than only the local one.
# memo_ai/pipeline.py normalizes and probes before it transcribes, because the duration of a
# browser recording cannot be read off the original file -- so a worker talking to a hosted
# provider still shells out to ffmpeg and ffprobe first.
#
# This layer is by far the most expensive thing in the image: **422 MB across 198 packages**,
# measured by diffing dpkg against the same base without these three. Almost none of it is
# audio code -- libavfilter pulls libplacebo pulls Mesa, and libllvm19 alone is 118 MB. The
# distro package is kept rather than swapping in static binaries, and deploy/README.md has
# that trade in full, including why ai/Dockerfile's reason for the same decision does not
# transfer to an image with no model weights to dwarf it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        postgresql-client \
        ffmpeg \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN install-php-extensions pdo_pgsql intl

COPY api/conf.d/ $PHP_INI_DIR/conf.d/

WORKDIR /app

COPY --from=composer:2 /usr/bin/composer /usr/local/bin/composer

COPY api/ ./
COPY --from=vendor /api/vendor ./vendor

RUN composer dump-autoload --no-dev --optimize && composer clear-cache

# The built SPA lands beside Laravel's front controller. Both live in public/ and neither
# shadows the other: deploy/Caddyfile sends /api/* to index.php and everything else to
# index.html, so the only file they both want -- the directory index -- is claimed explicitly
# by whichever handler matched.
COPY --from=web /web/dist/ ./public/

# The migrations and the runner, at the path db/migrate.sh resolves MIGRATIONS_DIR against.
COPY db/ /db/

# --------------------------------------------------------------------------
# The worker, in this container rather than beside it
# --------------------------------------------------------------------------
#
# This is the part that makes the deployment a demonstration instead of a screenshot, and it
# is here because of an arithmetic that only applies to free tiers.
#
# Without a worker *nothing* reaches `ready`. That is worth stating plainly because it is
# easy to assume only voice memos need one: a typed memo is written `queued` too, and it is
# the worker that moves it on. So an API-only deployment does not degrade to "text works,
# voice doesn't" -- every card on the page sits at "Waiting for a worker…" and polls forever.
# Measured against this image before the worker was added to it, which is how the claim that
# it degraded gracefully turned out to be wrong.
#
# The obvious fix is a second service. Free tiers are exactly where that is hardest: Render
# bills background workers from the first one, and the allowances elsewhere are sized for a
# single always-on process. So the worker runs here, and what makes that reasonable rather
# than a hack is its cost with the models externalized -- **41 MB RSS**, measured on the
# shipped configuration, against the ~2.4 GB per replica the local model needs. It is
# network-bound on Groq and idle the rest of the time.
#
# It stays a normal `python -m memo_ai.worker` with nothing forked into the PHP process, so
# the standalone shape still works: run this same image with `--entrypoint` pointed at the
# venv to get a worker container and nothing else. deploy/README.md has that command.

# The venv before the source, so editing memo_ai does not reinstall the driver. PEP 668 is
# why this is a venv at all -- see the apt layer above.
COPY ai/requirements-hosted.txt /opt/memo-ai/
RUN python3 -m venv /opt/memo-ai/venv \
    && /opt/memo-ai/venv/bin/pip install --no-cache-dir --disable-pip-version-check \
        -r /opt/memo-ai/requirements-hosted.txt

# Source only. No /opt/models and no HF_HOME: this image has no weights and must never be
# able to fetch any, because a worker that quietly downloaded 2.8 GB into a free tier's
# ephemeral disk on its first voice memo would be a worse failure than the honest one
# STT_PROVIDER=groq gives when its key is missing.
COPY ai/memo_ai/ /opt/memo-ai/memo_ai/

# Compiled here, as root, because the worker cannot do it for itself and fails silently
# trying. /opt/memo-ai is root-owned and the worker runs as `memo`, so every import attempts
# a .pyc write into a directory it cannot write and Python swallows the EACCES -- which means
# the whole package recompiles from source on each start rather than once at build.
#
# Measured rather than assumed, because a silent failure is exactly the kind that gets
# asserted away: importing memo_ai.config as root leaves 2 .pyc files behind, and as `memo`
# it leaves 0.
#
# `|| true` because compileall exits nonzero on a syntax error in *any* file it walks, and
# refusing to build the API over that would be the wrong trade -- the worker would fail
# loudly at import anyway, where the traceback is.
RUN /opt/memo-ai/venv/bin/python -m compileall -q /opt/memo-ai/memo_ai || true

COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY deploy/entrypoint.sh /usr/local/bin/memo-entrypoint

ARG APP_UID=10001
ARG APP_GID=10001
ARG AUDIO_DIR=/data/audio

RUN chmod +x /usr/local/bin/memo-entrypoint \
    && groupadd --gid ${APP_GID} memo \
    && useradd --uid ${APP_UID} --gid ${APP_GID} --home-dir /home/memo \
        --shell /usr/sbin/nologin memo \
    && mkdir -p ${AUDIO_DIR} /home/memo \
    && chown ${APP_UID}:${APP_GID} ${AUDIO_DIR} /home/memo \
    && chmod 2775 ${AUDIO_DIR} \
    && chown -R ${APP_UID}:${APP_GID} /data/caddy /config/caddy /config/composer \
    && chown -R ${APP_UID}:${APP_GID} /app/storage /app/bootstrap/cache

ENV HOME=/home/memo

USER memo

ENV PGCONNECT_TIMEOUT=2

# APP_DEBUG stays off. It is not only noise in a log: with it on, Laravel's 500 body carries
# the exception message, the file, the line and a full trace -- and web/src/api/request.js
# renders a 4xx `message` verbatim to the user. api/config has the fuller argument.
ENV APP_ENV=production \
    APP_DEBUG=false \
    LOG_CHANNEL=stderr

# Overridden by the platform when it dictates a port -- see deploy/Caddyfile.
ENV SERVER_NAME=":8080"

# --- The worker's defaults ---------------------------------------------------
#
# Set here rather than left to memo_ai/config.py, because that file's defaults are correct
# for the image it ships in and wrong for this one. It defaults STT_PROVIDER and STT_FALLBACK
# to `local` and ENRICH_PROVIDER to `local`, which is right when 2.8 GB of weights are baked
# in and impossible here.
#
# STT_FALLBACK matters as much as STT_PROVIDER and is the easier one to forget. Left at
# `local`, every Groq failure -- an expired key, a rate limit -- would fall through to a model
# this image does not contain, and the memo's recorded error would name the wrong provider.
# Set equal to STT_PROVIDER it collapses to a single provider instead of a chain, which
# memo_ai/stt/__init__.py does deliberately so the same provider is not called twice.
#
# ENRICH_PROVIDER=none costs the titles, summaries, tags and categories a local LLM would
# write. Memos still get a title -- memo_ai/titles.py falls back to the first line of the
# transcript -- so the list stays readable rather than becoming a wall of "Untitled".
# There is no hosted enricher to point at instead; that seam exists for STT only.
ENV STT_PROVIDER=groq \
    STT_FALLBACK=groq \
    ENRICH_PROVIDER=none

# The other half of the compileall above: the .pyc are built and the runtime must stop trying
# to write more. ai/Dockerfile sets this for the same reason and states it plainly -- turning
# the write off "makes that a decision instead of a swallowed EACCES". Without it the failure
# is invisible, which is what made it survive the first pass on this image.
ENV PYTHONDONTWRITEBYTECODE=1

# On by default, and that is the deliberate reading of "degrade honestly".
#
# With it off, a deployment that forgot GROQ_API_KEY looks *identical* to a healthy one until
# you record something, and then waits forever with no error anywhere. With it on, the same
# deployment serves typed memos completely and fails a voice memo in a few seconds with a
# sentence naming the missing variable. Both are degraded; only one is diagnosable.
#
# Set RUN_WORKER=false to turn it off -- worth doing only when a worker is deployed
# separately, or the two compete for the same queue. That is safe (claims are atomic, see
# memo_ai/memos.py) but it doubles the polling for no gain.
ENV RUN_WORKER=true

EXPOSE 8080

ENTRYPOINT ["memo-entrypoint"]

CMD ["frankenphp", "run", "--config", "/etc/caddy/Caddyfile"]
