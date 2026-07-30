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

# libpq reads PGCONNECT_TIMEOUT and applies it as the default connect_timeout.
# This is the only place to bound it: Laravel's PostgresConnector builds the DSN
# itself and has no connect_timeout key, and pdo_pgsql ignores PDO::ATTR_TIMEOUT.
# libpq's own default is to wait indefinitely, while the compose healthcheck gives
# up after 3 seconds -- so without this a wedged database marks the container
# unhealthy with nothing in the logs to explain why.
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
