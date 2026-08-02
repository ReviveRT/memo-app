# The production image: one container serving the built SPA and the API from one origin.
#
# **Build this from the repository root**, not from api/ or web/ -- it needs both:
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
# Not included, deliberately: ai-worker and ai-api. The models they bake are ~2.8 GB and
# neither will run in a free tier's memory. The application degrades honestly without them --
# a voice memo stays `queued` and a text memo works completely -- and deploy/README.md has
# what to set to get transcription back through a hosted provider instead (STT_PROVIDER=groq).

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

# postgresql-client is the one addition over api/Dockerfile, and it is here for migrations.
# Compose runs those as a separate one-shot `migrate` service; a single container has nowhere
# to put one, so deploy/entrypoint.sh runs db/migrate.sh at boot and that script is psql.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl postgresql-client \
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

EXPOSE 8080

ENTRYPOINT ["memo-entrypoint"]

CMD ["frankenphp", "run", "--config", "/etc/caddy/Caddyfile"]
