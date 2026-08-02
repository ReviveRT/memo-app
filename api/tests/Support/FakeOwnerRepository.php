<?php

declare(strict_types=1);

namespace Tests\Support;

use App\Repositories\OwnerRepository;
use App\Services\Owners\Owner;
use App\Support\OwnerToken;

/**
 * Stands in for OwnerRepository so the suite can run the real ResolveOwner middleware
 * without Postgres.
 *
 * Bound for every test by Tests\TestCase rather than per test file, because owner resolution
 * is now global middleware: without it *every* feature test would fail on the first query
 * OwnerRepository issues, and the real one is Postgres-specific (to_char, RETURNING) while
 * the suite runs on sqlite in memory.
 *
 * Binding a fake rather than skipping the middleware is the point. The tests still exercise
 * the actual cookie handling -- minting, the transient path for a cookie-less read, the
 * once-a-day refresh -- so the wiring in bootstrap/app.php is covered by everything that
 * already existed, instead of being the one layer nothing touches.
 *
 * The parent constructor is deliberately not called, matching FakeMemoRepository:
 * DatabaseManager is the thing this class exists to avoid, and every method that would have
 * used it is overridden.
 */
final class FakeOwnerRepository extends OwnerRepository
{
    /**
     * Owners that exist, keyed by token hash.
     *
     * @var array<string, Owner>
     */
    private array $owners = [];

    /** Every insert() call, in order, as {id, token_hash}.
     *
     * @var list<array{id: string, token_hash: string}>
     */
    public array $inserted = [];

    /** Every touch() call, in order, as the owner id.
     *
     * @var list<string>
     */
    public array $touched = [];

    /**
     * A timestamp far enough in the past that ResolveOwner's staleness check fires, or recent
     * enough that it does not. Tests that care about the refresh set this before the request.
     */
    public string $lastSeenAt = '2099-01-01T00:00:00.000Z';

    public function __construct() {}

    /**
     * Make a token resolve to an owner, as though this browser had been here before.
     *
     * Takes the plaintext token and hashes it the same way the middleware will, so a test
     * writes the value it is going to put in the cookie rather than a hash it computed by
     * hand -- which is the kind of duplication that makes a test pass for the wrong reason.
     */
    public function give(string $token, string $ownerId): Owner
    {
        $owner = new Owner($ownerId, $this->lastSeenAt);

        $this->owners[OwnerToken::hash($token)] = $owner;

        return $owner;
    }

    public function findByTokenHash(string $tokenHash): ?Owner
    {
        return $this->owners[$tokenHash] ?? null;
    }

    public function insert(string $id, string $tokenHash): Owner
    {
        $this->inserted[] = ['id' => $id, 'token_hash' => $tokenHash];

        return $this->owners[$tokenHash] = new Owner($id, $this->lastSeenAt);
    }

    public function touch(string $id, string $notSeenSince): void
    {
        $this->touched[] = $id;
    }

    /**
     * @return array{owners: int, memos: int, recordings: int}
     */
    public function prunable(string $notSeenSince): array
    {
        return ['owners' => 0, 'memos' => 0, 'recordings' => 0];
    }

    /**
     * @return array<int, string>
     */
    public function prune(string $notSeenSince): array
    {
        return [];
    }
}
