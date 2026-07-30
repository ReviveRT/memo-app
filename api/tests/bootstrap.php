<?php

declare(strict_types=1);

/*
|--------------------------------------------------------------------------
| PHPUnit bootstrap
|--------------------------------------------------------------------------
|
| This project has no .env file and does not want one: every value arrives as a
| container environment variable, docker-compose.yml carries the dev defaults
| inline, and MEMO-01 forbids committing one at all.
|
| Laravel probes for it on every boot regardless. Dotenv reads it through an
| @-suppressed file_get_contents, which is correct behaviour on its part, but
| `php artisan test` installs an error handler that reports suppressed PHP
| warnings anyway -- turning three passing tests into three yellow warnings about
| a file whose absence is the design. PHPUnit's own
| ignoreSuppressionOfPhpWarnings does not reach it, because the handler doing the
| reporting is Laravel's, not PHPUnit's. Verified: `vendor/bin/phpunit` is clean
| either way, `php artisan test` is not.
|
| Touching an empty, gitignored .env is the smallest thing that makes both entry
| points clean on a fresh clone. Dotenv is immutable and the file is empty, so it
| cannot override a single value the container already set.
|
*/

require __DIR__.'/../vendor/autoload.php';

$envFile = __DIR__.'/../.env';

if (! file_exists($envFile)) {
    touch($envFile);
}
