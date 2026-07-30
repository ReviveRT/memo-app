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
RUN install-php-extensions pdo_pgsql

# Into $PHP_INI_DIR (/usr/local/etc/php), which is outside /app and therefore not
# shadowed by the ./api:/app bind mount compose uses for live editing. An ini file
# left at /app/conf.d would be ignored, and the 2M upload default would be back
# with no visible change to this file.
#
# The image ships no php.ini at all, so these two files are the entire PHP
# configuration. Read them both before assuming a default is in effect.
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
RUN composer dump-autoload --no-dev --optimize

# Compose overrides this, but it also has to be right for a bare `docker run`.
# A port-only SERVER_NAME keeps Caddy on plain HTTP; a hostname would send it
# looking for a certificate.
ENV SERVER_NAME=":8080"
EXPOSE 8080
