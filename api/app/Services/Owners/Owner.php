<?php

declare(strict_types=1);

namespace App\Services\Owners;

use RuntimeException;
use stdClass;

/**
 * Who a request belongs to.
 *
 * Two fields and no behaviour, because there is genuinely nothing else known about an
 * owner: no name, no email, no preferences. That is the whole design -- see
 * db/migrations/007_owners.sql for why this application has owners and not accounts.
 *
 * The token is *not* on this object, deliberately. An owner is resolved by hashing the
 * cookie and looking the hash up, so after that point the plaintext is not needed and
 * carrying it here would put a bearer secret into every service that takes an Owner,
 * into any dump of one, and into any exception that renders its arguments. The one
 * operation that does need it -- minting the claim link -- gets it directly from the
 * middleware that read the cookie, and nowhere else.
 */
final class Owner
{
    private const REQUIRED_COLUMNS = ['id', 'last_seen_at_iso'];

    public function __construct(
        public readonly string $id,

        /**
         * When this owner was last seen, as the same RFC 3339 string every other timestamp
         * on the wire uses.
         *
         * Read by ResolveOwner to decide whether today's visit is worth a write. It is on
         * the object rather than fetched separately because the resolve query has to select
         * the row anyway, so it costs one more column on a query that already runs once per
         * request.
         */
        public readonly string $lastSeenAt,
    ) {}

    public static function fromRow(stdClass $row): self
    {
        foreach (self::REQUIRED_COLUMNS as $column) {
            if (! property_exists($row, $column)) {
                throw new RuntimeException(
                    "Owner row is missing the column {$column}: OwnerRepository and Owner::fromRow disagree."
                );
            }
        }

        return new self(
            id: (string) $row->id,
            lastSeenAt: (string) $row->last_seen_at_iso,
        );
    }
}
