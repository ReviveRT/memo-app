<?php

declare(strict_types=1);

/*
|--------------------------------------------------------------------------
| Cross-Origin Resource Sharing (CORS)
|--------------------------------------------------------------------------
|
| This file exists to switch CORS off. Laravel does not publish it by default,
| and the framework defaults are `paths => ['api/*', 'sanctum/csrf-cookie']` with
| `allowed_origins => ['*']` -- so the stock skeleton answers every /api/* request
| with `Access-Control-Allow-Origin: *`. Confirmed against this container before
| this file existed.
|
| That is wrong for this project twice over.
|
| It is unnecessary: the Vue dev server proxies /api to api:8080 (MEMO-07), so the
| browser only ever sees one origin and there is no cross-origin request to
| permit. That proxy is the CORS answer, and it is the reason there is no
| preflight to handle either.
|
| And it is not free. There is no authentication anywhere in this project
| (MEMO-27 lists that as a deliberate cut), so every endpoint is readable by
| whoever can reach it. A wildcard ACAO header additionally invites any page in
| the user's browser to read memo transcripts from a running instance. The header
| costs nothing to remove and buys nothing to keep.
|
| An empty `paths` array makes HandleCors a no-op rather than removing the
| middleware, so a future genuine cross-origin consumer is one line of config away
| instead of an archaeology exercise.
|
*/

return [

    'paths' => [],

    'allowed_methods' => ['*'],

    'allowed_origins' => [],

    'allowed_origins_patterns' => [],

    'allowed_headers' => ['*'],

    'exposed_headers' => [],

    'max_age' => 0,

    'supports_credentials' => false,

];
