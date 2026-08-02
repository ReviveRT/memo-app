<?php

declare(strict_types=1);

namespace Tests\Support;

use App\Contracts\AskBackend;
use App\Exceptions\AskUnavailable;

/**
 * An AskBackend that answers with whatever chunks it was handed, or refuses.
 *
 * The real one talks to a container running a 1.5 GB model, so this is not faked for speed --
 * it is faked because `php artisan test` has no such container and never will. What the
 * substitution can show is everything about the *route*: that a refusal before the first byte
 * is a 503 and not an empty 200, that the answer reaches the client byte-for-byte with nothing
 * parsed or re-serialised on the way, and that the question the model is asked is the trimmed
 * one the rules produced. What it cannot show is whether the answer is any good, which is not
 * something an HTTP test could judge in any case.
 *
 * `$chunks` is a list rather than a string, and the split points matter: it is what lets a test
 * assert that a JSON object arriving in two pieces still reaches the client whole. A backend
 * yielding one chunk per response would make the pass-through look correct for the one case it
 * is least likely to be wrong about.
 */
final class FakeAskBackend implements AskBackend
{
    /** Every question asked, in order. @var list<string> */
    public array $asked = [];

    /**
     * What to answer with, in the pieces to answer in.
     *
     * @var list<string>
     */
    public array $chunks = [];

    /** Set to refuse before answering, standing in for an ai-api that is down or loading. */
    public ?string $unavailable = null;

    public function ask(string $question): iterable
    {
        $this->asked[] = $question;

        if ($this->unavailable !== null) {
            // Thrown from `ask` itself rather than from the generator below, which is the
            // whole property the real class is careful about: everything worth a status code
            // has to happen before the controller returns a response. A double that raised
            // lazily would let a broken implementation pass.
            throw new AskUnavailable($this->unavailable);
        }

        return $this->stream();
    }

    /** @return iterable<string> */
    private function stream(): iterable
    {
        yield from $this->chunks;
    }
}
