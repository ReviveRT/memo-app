# syntax=docker/dockerfile:1

# FrankenPHP, not `php -S`. The built-in server is single-threaded, so one client
# holding a streaming audio response would stall every status poll behind it --
# the UI would look frozen for reasons nothing in the application code explains.
#
# Pinned to the 1.x line on php 8.3 so a rebuild cannot move the PHP minor
# version under a lock file resolved against 8.3.
FROM dunglas/frankenphp:1-php8.3

# curl is what the compose healthcheck shells out to, and `web` only starts once
# that healthcheck passes. Installed explicitly rather than assumed from the base
# image: if it disappeared upstream the failure would be a stack that hangs with
# api marked unhealthy and nothing in the logs to say why.
#
# unzip is a build-time need with a confusing error attached. The base image has
# neither the zip extension nor an unzip binary, and `composer install
# --prefer-dist` then fails with "The zip extension and unzip/7z commands are both
# missing, skipping." followed by the install usage text -- which reads like a bad
# flag rather than a missing package.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip \
    && rm -rf /var/lib/apt/lists/*

# pdo_pgsql is not in the base image. install-php-extensions ships with
# frankenphp and pulls the libpq headers for the build, then drops them.
#
# intl is Laravel's, not ours -- no application code calls it. It is here because
# the framework's own helpers do: without it `php artisan db:show` prints its table
# and then dies with `The "intl" PHP extension is required to use the [format]
# method` from Illuminate\Support\Number. Observed on this image. Leaving a
# first-party artisan command broken is a worse trade than the ICU data it pulls
# in, on an api image that is a rounding error next to the 1.2 GB ai image.
RUN install-php-extensions pdo_pgsql intl

# Into $PHP_INI_DIR (/usr/local/etc/php), which is outside /app and therefore not
# shadowed by the ./api:/app bind mount compose uses for live editing. An ini file
# left at /app/conf.d would be ignored, and the 2M upload default would be back
# with no visible change to this file.
#
# The image ships no php.ini at all, so these three files are the entire PHP
# configuration. Read them before assuming a default is in effect -- env.ini in
# particular exists because Laravel cannot see the container's environment
# without it.
COPY conf.d/ $PHP_INI_DIR/conf.d/

WORKDIR /app

COPY --from=composer:2 /usr/bin/composer /usr/local/bin/composer

# Dependencies before application code, so editing a controller does not
# re-resolve the dependency tree. composer.lock* -- the glob tolerates its
# absence, and composer.json pins config.platform to php 8.3 so resolution gives
# the same answer in this builder as at runtime.
COPY composer.json composer.lock* ./
RUN composer install \
        --no-dev \
        --no-interaction \
        --no-progress \
        --prefer-dist \
        --no-scripts \
        --no-autoloader \
    && composer clear-cache

COPY . .

# --optimize builds a classmap but keeps the PSR-4 fallback; --classmap-authoritative
# would drop it, and with ./api bind-mounted a class added on the host would then
# fail to load until the image was rebuilt.
#
# This also runs Laravel's post-autoload-dump hook, which is what writes
# bootstrap/cache/packages.php. Deliberately no `artisan config:cache` or
# `route:cache`: config:cache resolves every env() call at cache time, and this
# image is built long before DATABASE_URL or AUDIO_DIR exist -- so a cached config
# would freeze the build-time defaults into the image and ignore compose entirely.
RUN composer dump-autoload --no-dev --optimize

# --- The shared-volume uid/gid contract (MEMO-12) ----------------------------
#
# The `audio` volume is written by this container and read *and unlinked* by the
# Python worker. unlink(2) checks write permission on the containing directory,
# not on the file -- so a fresh named volume left at Docker's default root:root
# 0755 gives the worker EACCES on delete while read keeps working. Reproduced
# before this block existed: a non-root worker `cat`s the file fine and then gets
# "rm: cannot remove '/data/audio/a.webm': Permission denied".
#
# The decision, made once: both containers on this volume run as uid 10001 /
# gid 10001, named `memo`. 10001 is clear of the distro system range and of the
# 1000 a host user would occupy on a Linux checkout. docker-compose.yml repeats
# the two numbers in one anchor for both services -- deliberately mirrored rather
# than derived, the same way config/memo.php mirrors its defaults, because this
# image also has to be right under a bare `docker run`. NOTES.md records why.
#
# The premise in the ticket does not hold for this image and was worth checking
# before fixing: `dunglas/frankenphp:1-php8.3` runs as **root**, not www-data, and
# so does `python:3.12-slim`. Both sides therefore agreed by accident, with nothing
# recording it. A `USER` line added to the worker would have broken that quietly
# and only on delete; one added here would have broken it loudly, since the API
# could not write at all. The quiet half is the one worth pinning.
#
# Args rather than literals so this image can be rebased without editing the body,
# but they are not independent knobs: the uid and gid have to match the
# x-audio-user anchor in docker-compose.yml, and AUDIO_DIR has to match the path
# compose mounts the volume at or the seeding below prepares a directory nothing
# ever mounts. Changing either means editing both files; .env.example says so at
# the variable.
ARG APP_UID=10001
ARG APP_GID=10001
ARG AUDIO_DIR=/data/audio

# Four things, and only the first is obvious:
#
#  1. ${AUDIO_DIR} is pre-created *in the image* so Docker seeds the named volume
#     from it -- it copies the mount point's uid, gid and mode out of the image.
#     Verified: an `audio` volume comes up drwxrwsr-x memo memo instead of the
#     drwxr-xr-x root root Docker would otherwise leave.
#
#     The condition is that the volume is **empty**, which is not the same as new:
#     a volume left behind by a pre-MEMO-12 build is re-seeded on the next start
#     and heals itself. One that already holds blobs does not, and there
#     `docker compose down -v` is the only reset. NOTES.md tabulates all four
#     cases; they were checked, in both container orders, not reasoned about.
#
#     2775 rather than 0775: the setgid bit makes every file and subdirectory
#     either container creates inherit the `memo` group, so a worker running under
#     a different uid but with `memo` as a supplementary group can still unlink.
#     The group-write bit is the part that was missing and the part that matters --
#     0755 already grants the read and traverse that a worker needs, which is
#     exactly why this failed on delete alone and looked healthy from the outside.
#
#  2. Caddy's own state. XDG_DATA_HOME=/data and XDG_CONFIG_HOME=/config in this
#     image, and Caddy provisions a local CA on startup even with a port-only
#     SERVER_NAME and no TLS in play. Without this chown the non-root server exits
#     before it ever listens: "loading pki app module: provisioning CA 'local':
#     generating root: saving root certificate: mkdir /data/caddy/pki: permission
#     denied". Both directories already exist in the base image and are empty.
#
#     Note that /data is also the parent of the audio mount point. That is the
#     base image's choice of XDG_DATA_HOME, not ours; the volume at /data/audio
#     and Caddy's /data/caddy do not overlap.
#
#  3. composer ran as root above and `package:discover` wrote
#     bootstrap/cache/{packages,services}.php as root. Both are read on every
#     request and rewritten whenever bootstrap/providers.php changes -- and that
#     file is inside the ./api bind mount, so it can change without a rebuild.
#     Left root-owned, adding a provider turns every request into a 500 reading
#     "The bootstrap/cache directory must be present and writable". This one is
#     load-bearing under compose: bootstrap/cache is an anonymous volume, so it
#     carries the ownership set here rather than the host's.
#
#     storage/ is chowned for the bare `docker run` case only, and is included for
#     that reason rather than because compose needs it -- under compose the
#     ./api bind mount covers that path and the host decides who owns it. Nothing
#     in this API writes there today (LOG_CHANNEL is stderr, no sessions, no file
#     cache, no compiled views), which is why the difference has no symptom yet.
#
#  4. HOME, plus composer's idea of it. The base image points HOME at /root, which
#     uid 10001 cannot read, and compose bind-mounts ./api precisely so people work
#     inside this container. Composer does not take its home from HOME on Linux
#     though -- it reads XDG_CONFIG_HOME, which this image sets to /config, and the
#     build-time `composer install` above already created /config/composer as root.
#     Without the chown, `docker compose exec api composer …` fails on "touch():
#     Unable to create file /config/composer/config.json because Permission
#     denied". Observed, hence both paths here rather than just the tidy one.
#
# Not `--system`: Debian's SYS_UID_MAX is 999, so `useradd -r -u 10001` creates the
# account correctly and then warns about it on every single build. A regular
# account with an explicit uid, no password and a nologin shell is the same thing
# without the noise.
RUN groupadd --gid ${APP_GID} memo \
    && useradd --uid ${APP_UID} --gid ${APP_GID} --home-dir /home/memo \
        --shell /usr/sbin/nologin memo \
    && mkdir -p ${AUDIO_DIR} /home/memo \
    && chown ${APP_UID}:${APP_GID} ${AUDIO_DIR} /home/memo \
    && chmod 2775 ${AUDIO_DIR} \
    && chown -R ${APP_UID}:${APP_GID} /data/caddy /config/caddy /config/composer \
    && chown -R ${APP_UID}:${APP_GID} /app/storage /app/bootstrap/cache

ENV HOME=/home/memo

# Everything above this line ran as root on purpose: /app/vendor stays root-owned
# and read-only to the running application, which is a property worth keeping.
USER memo

# libpq's documented default for connect_timeout, set here because Laravel's
# PostgresConnector builds the DSN itself and has no connect_timeout key, and
# pdo_pgsql ignores PDO::ATTR_TIMEOUT.
#
# Do not rely on this bounding anything. Measured against a blackholed address
# (10.255.255.1) from this image, the connect fails after 30.0s at values of 2, 3,
# 5, 60 and with the variable unset entirely -- identical every time, so neither
# this variable nor a connect_timeout key in the DSN has any observable effect on
# that path. The 30s is imposed by the Docker Desktop network stack, not by libpq.
# It is kept because it is the correct mechanism and may well apply on a host that
# is not Docker Desktop, not because it was shown to work.
#
# What actually bounds the healthcheck is curl: `timeout: 3s` in
# docker-compose.yml aborts the probe, the check fails, and the container is
# correctly marked unhealthy. The failure modes that matter in this stack are all
# fast anyway -- a stopped db container refuses the connection immediately, a bad
# hostname fails DNS immediately, bad credentials are rejected immediately. Only a
# route that silently drops packets hangs, which needs a firewall between two
# containers on the same compose network.
#
# The open consequence: a human curling /api/health in that one scenario waits for
# the hang instead of getting a fast diagnostic 503. Fixing that properly means a
# short bounded TCP reachability probe in HealthRepository before handing over to
# PDO. Not built, because it trades real complexity against a failure mode this
# topology cannot easily produce -- but that is where it goes.
ENV PGCONNECT_TIMEOUT=2

# Sane defaults for a bare `docker run`; compose sets these explicitly too.
# APP_DEBUG stays off even for local use: a debug response carries the stack trace
# and the resolved configuration, and /api/* is reachable through the web
# container's proxy. The detail goes to stderr instead, via LOG_CHANNEL.
ENV APP_ENV=local \
    APP_DEBUG=false \
    LOG_CHANNEL=stderr

# Compose overrides this, but it also has to be right for a bare `docker run`.
# A port-only SERVER_NAME keeps Caddy on plain HTTP; a hostname would send it
# looking for a certificate.
ENV SERVER_NAME=":8080"
EXPOSE 8080
