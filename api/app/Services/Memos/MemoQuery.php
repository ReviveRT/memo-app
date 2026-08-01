<?php

declare(strict_types=1);

namespace App\Services\Memos;

use App\Support\TimeWindow;

/**
 * Everything the memo list can be narrowed by, in one object.
 *
 * It exists because the list grew from two parameters to five, and the two shapes it used
 * to have -- `recent($limit)` and `search($query, $limit)` -- do not survive that. With a
 * text filter, a date window and a collection scope all independently optional there are
 * eight combinations, and a repository method per combination is not something anyone can
 * keep right. So there is one statement assembled from optional predicates, and this is
 * what says which of them apply.
 *
 * Five positional arguments would have done the same job, and that is what this replaces:
 * `recent(null, $from, $to, null, true, 50)` says nothing at the call site and is
 * silently wrong if two arguments of the same type are swapped. Two of these fields are
 * `?string` and one is a bool, so that risk is real rather than theoretical.
 *
 * `$window` has no default, unlike everything else here. `= TimeWindow::unbounded()` is
 * not available -- a parameter default must be a constant expression, and PHP's
 * new-in-initializers allows `new C(...)` but not a static call -- and the alternative of
 * making TimeWindow's constructor public to get `new TimeWindow(null, null)` would expose
 * a way to build one whose ends were never normalised to UTC. Requiring it costs every
 * caller the words `window: TimeWindow::unbounded()` and keeps normalisation the only way
 * in.
 */
final class MemoQuery
{
    /**
     * @param  TimeWindow  $window  Created-at bounds. Half-open; see the class.
     * @param  ?string  $text  The search box, already trimmed and non-empty, or null for no
     *                         text filter. Capped by ListMemosRequest.
     * @param  ?string  $collectionId  One collection's memos, or null for "not scoped to a
     *                                 single collection" -- which is not the same thing as
     *                                 $unfiledOnly.
     * @param  bool  $unfiledOnly  Only memos in no collection at all: the fast strip. Set
     *                             with $collectionId it would ask for a memo that is both
     *                             in a collection and in none, so ListMemosRequest spells
     *                             the two as one `?collection=` parameter that cannot say
     *                             both.
     * @param  int  $limit  Already validated against ListMemosRequest::MAX_LIMIT.
     */
    public function __construct(
        public readonly TimeWindow $window,
        public readonly ?string $text = null,
        public readonly ?string $collectionId = null,
        public readonly bool $unfiledOnly = false,
        public readonly int $limit = 50,
    ) {}
}
