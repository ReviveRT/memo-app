<?php

declare(strict_types=1);

namespace App\Exceptions;

use RuntimeException;

/**
 * Nothing answered the question, before a single byte of an answer had gone out.
 *
 * **503, not 500**, which is the distinction this class exists to draw and the same one
 * App\Exceptions\StorageException draws on the upload path. A 500 says this application is
 * broken; the application is fine, and one optional service it proxies to is either not
 * running or still loading a model. The difference matters because the second one has an
 * obvious next step -- `docker compose up ai-api`, or wait thirty seconds -- and the first
 * one does not.
 *
 * **Only reachable before the response has begun**, and that is a property of the route
 * rather than of this class: AskBackend::ask establishes the connection eagerly, so
 * everything that can be a status code has already happened by the time the controller
 * returns a stream. A failure after that point cannot be one -- 200 is already committed --
 * and travels as an `error` event inside the NDJSON instead. AskController has both halves.
 *
 * **What its message is for.** It reaches `curl -i` and the api log, and it is written for a
 * person -- no URL, no host, no driver text, on App\Exceptions\StorageException's rule. It
 * does *not* reach the browser: web/src/api/request.js replaces the body of every 5xx with a
 * sentence of its own (MEMO-17), because a 5xx body is normally either Laravel's useless
 * "Server Error" or, with APP_DEBUG on, a stack trace. That rule is right and this route does
 * not carve an exception out of it -- web/src/api/ask.js authors its own sentence for a 503,
 * naming the ai-api container, which is the one thing the reader can act on.
 */
final class AskUnavailable extends RuntimeException {}
