# Notes

Decisions and trade-offs that the code cannot state for itself.

> **Status: partial.** MEMO-27 owns this file and writes the rest of it. The two
> entries below are here early for the same reason: each is a contract that
> several files — and, in both cases, two different runtimes — have to agree
> about, so it cannot live in any one of them.

## `updated_at` is maintained by a trigger, not by convention

**Decision.** A `BEFORE UPDATE ... FOR EACH ROW` trigger on `memos` sets
`updated_at = now()`. It lives in `db/migrations/002_updated_at.sql`, and no
UPDATE anywhere — PHP, Python, or a hand-rolled `psql` session — needs to mention
the column. MEMO-06 owned this choice; `001_init.sql` deliberately left it open.

**Why it needs deciding at all.** Postgres has no `ON UPDATE
CURRENT_TIMESTAMP`. `updated_at timestamptz NOT NULL DEFAULT now()` covers the
INSERT and nothing else, which was checked rather than assumed: an UPDATE that
changes `status` leaves `updated_at` at its insert value. So the column is either
maintained explicitly by every writer or maintained by the database, and the one
outcome that must not happen is a column that looks maintained and is not — a
stale timestamp is indistinguishable from a fresh one.

**Why a trigger and not `updated_at = now()` in every UPDATE.** Two runtimes
write this table, and the convention was already broken by the next task in the
build order before it was written down. MEMO-08's claim statement is

```sql
UPDATE memos SET status='processing', locked_at=now(), attempts=attempts+1
WHERE id = (...) RETURNING id, source, transcript, audio_path, attempts, locked_at
```

which never mentions `updated_at`. Under a convention that is a silent bug in
Python, found by nobody, in a codebase whose author has no reason to read the PHP
repository. That exact statement was run against this schema with the trigger
installed: `updated_at` moved, and it moves in the shipped worker too. A trigger
cannot be forgotten by a new writer, a second worker replica, or a future `ai-api`.

The projection differs from the `RETURNING *` this entry first quoted, and for a
reason that belongs with the column list rather than here: `search_vector` is a
STORED generated column and therefore part of `*`, so the one statement that runs
on every poll of an empty queue would have carried a full stemmed copy of every
transcript. `ai/memo_ai/memos.py` and `MemoRepository::COLUMNS` state that rule on
their own sides.

**What this column is still not good for.** It is not a delta cursor, and the
trigger does not make it one. `now()` is transaction-start time, so a row whose
write transaction began before a poll read and committed after it carries an
`updated_at` the poller has already passed — the row is skipped, silently. That
is why MEMO-18 polls the whole visible page and replaces rows by id instead of
asking for a `?since=`. The trigger makes `updated_at` truthful about *when this
row was last written*; a delta feed needs a monotonic sequence, not a clock.

**What was rejected.**

- *`updated_at = now()` in every UPDATE.* Visible in the SQL, and that is its
  only advantage. See above for the reason it was never going to hold.
- *`WHEN (OLD.* IS DISTINCT FROM NEW.*)` on the trigger*, the usual guard against
  bumping the timestamp on a no-op UPDATE. It is broken on this table
  specifically, and quietly: in a BEFORE trigger the STORED generated column
  `search_vector` is not yet computed, so `NEW.search_vector` is NULL while
  `OLD.search_vector` holds the old vector. Confirmed by raising a NOTICE from a
  throwaway trigger — the rows compare as distinct even for `SET status = status`,
  so the guard suppresses nothing while reading like an optimisation.
- *A `updated_at` column maintained by the API only.* The API is not the only
  writer, and the worker does strictly more UPDATEs than it does.

## The shared `audio` volume runs as uid 10001, gid 10001

**Decision.** Every container that mounts the `audio` volume runs as
`10001:10001`, a user named `memo`. `AUDIO_DIR` is `2775` (group-writable,
setgid) and the blobs inside it are `0664`. The numbers live in
`api/Dockerfile` (as `APP_UID` / `APP_GID` build args) and in the
`x-audio-user` anchor in `docker-compose.yml`. `ai/Dockerfile` landed with MEMO-08
and creates the same user; the cross-container half of the acceptance criterion has
now been run for real rather than reasoned about — the api container writes a blob
and the worker container reads it and unlinks it, with `/data/audio` arriving
`drwxrwsr-x memo memo`.

**Why it needs deciding at all.** The API writes a blob and the worker reads it
and then deletes it. `unlink(2)` checks write permission on the *directory*, not
on the file — so two containers that disagree about uid/gid produce a worker that
can read every blob and delete none of them. Nothing logs, the memo transcribes
fine, and the volume fills up with files nobody owns. Reproduced before any of
this was written: the worker `cat`s the file and then gets `rm: cannot remove
'/data/audio/a.webm': Permission denied`.

**Why group-writable and not just a matching uid.** A matching uid alone would
work. The setgid directory and the `0664` blobs are the second line: they keep the
arrangement working for a container that runs as some *other* uid with `memo` as a
supplementary group, which is the shape a future ai image is most likely to arrive
in. Both identities were checked container-to-container on a fresh volume — same
uid, and group-only at uid 12345 — and the modes they depend on are pinned by
`api/tests/Unit/SharedAudioVolumeTest.php`.

**One thing MEMO-13 should know — and what MEMO-13 did with it.** A file the
*worker* creates on this volume comes out `0644` under the default umask, not
`0664`. Nothing today cares: the worker only ever reads and unlinks, and unlink is
permitted by the directory rather than by the file.

This paragraph used to predict that MEMO-13 would change that by writing a
normalized copy back to the volume. It does not, and the prediction is left here
rather than deleted because the reasoning against it is the useful part.
Normalization writes to a temporary directory on the container's own writable
layer and deletes it when the job ends (`ai/memo_ai/audio.py`). Three reasons, in
order of weight:

- The normalized file is a transient *input to transcription*, and nothing reads
  it afterwards. MEMO-23 serves playback from the original, which is the user's
  actual recording rather than a 24 kbps re-encode of it — MEMO-11 chose the
  upload bitrate on exactly that basis, to avoid stacking two lossy passes on the
  only copy.
- Keeping it would grow the volume by roughly a fifth per memo, permanently, for
  bytes nothing will open again.
- It keeps the worker a reader-and-unlinker on this volume, which is the property
  the whole uid/gid contract above is written around. A container that also
  *creates* files here is the one that has to care about the umask.

So the `0644` fact stands and is still worth knowing — it just applies to whoever
first writes a durable file here, and that is not this task.

**How the mode gets onto the volume.** `api/Dockerfile` creates `AUDIO_DIR` in
the image with the right owner and mode, and Docker copies a named volume's mount
point — uid, gid and mode — out of the image. So the first `docker compose up` on
a clean checkout comes up `drwxrwsr-x memo memo` rather than the `drwxr-xr-x root
root` Docker would otherwise create.

The exact rule matters, and every case here was checked rather than assumed.
Docker applies the image's owner and mode whenever the volume is **empty** at
container start — not only at creation:

| Volume state | Result |
| --- | --- |
| Never mounted | Seeded from the api image. Correct. |
| Never mounted, and `ai-worker` starts first | Docker creates the mount point itself: `root:root 0755`. Heals as soon as `api` starts — see the row below. |
| Empty, created by a pre-MEMO-12 (root) image, or by the ai image alone | Re-seeded on the next start. Self-heals. |
| Empty, already seeded, then mounted by an image without that path | Left alone. Still correct. |
| Holds blobs from a root-era build | Stays `root:root 0755`. The API cannot write. |

The second row is the one that was reasoned about first and measured later, once
`ai/Dockerfile` existed to measure. Bringing `ai-worker` up on its own gives a
`root:root 0755` `/data/audio`, because an image with no such path has nothing for
Docker to seed from; bringing `api` up afterwards re-seeds it to `drwxrwsr-x memo
memo`, still carrying the api image's build mtime. It is benign — the worker only
reads and unlinks, and an untouched volume holds nothing to read — but "the ai image
does not create the path" is not the same claim as "the ai image starting first
changes nothing", and only the first one is true.

Only the last row needs a human: `docker compose down -v`, or
`docker volume rm memo-app_audio` with the stack down to keep `pgdata`. The
failure is loud — every upload fails on the temp file — so nobody debugs it for
long.

The third row is why the ai image does **not** need to prepare `AUDIO_DIR` and
should not try: an image with no such path leaves an already-seeded volume alone,
but one that creates it with a *different* owner would win the seed whenever it
starts first, and api and ai-worker start concurrently.

**The consequence to know about.** The path is baked into the image, so
overriding `AUDIO_DIR` without rebuilding leaves the volume mounted somewhere
that was never prepared, and the API cannot write there. Change it in both
places or not at all: `.env` and
`docker compose build --build-arg AUDIO_DIR=…`.

**What was rejected.**

- *Leave both containers as root.* This is what the stack did before this task, by
  accident rather than by decision: `dunglas/frankenphp:1-php8.3` and
  `python:3.12-slim` both run as root, so they already agreed. Rejected because
  nothing recorded the agreement, and the two ways of breaking it are not equally
  visible — a `USER` line added to the *worker* breaks only delete, silently, while
  one added to the *api* stops it writing at all and is obvious within a second.
  The quiet half is the one that would have shipped. The API is also the only
  public surface in the stack and the only thing handling multipart uploads; it has
  no business being root.
- *`chown` the volume from an entrypoint.* Needs to start as root and drop
  privileges, which means an entrypoint wrapper and `gosu` in an image that
  currently needs neither.
- *`0777` on the directory.* Removes the need to agree on anything, and removes
  the reason to think about it again. The next person to add a container gets no
  signal at all.

**The one thing that made this more than a two-line change.** FrankenPHP as
non-root does not boot without help. Caddy provisions a local CA on startup even
with a port-only `SERVER_NAME` and no TLS anywhere, and it writes under
`XDG_DATA_HOME` and `XDG_CONFIG_HOME` — which this image sets to `/data` and
`/config`, both root-owned. Unfixed, the container exits before it listens with
`provisioning CA 'local': … mkdir /data/caddy/pki: permission denied`.

Those two directories are chowned in `api/Dockerfile` for that reason.
`bootstrap/cache`, Laravel's `storage/` and Composer's `/config/composer` are
chowned for three unrelated reasons of the same shape — root wrote them during the
build and a non-root process has to write them at runtime — and each carries the
failure it prevents in a comment next to it. Composer's is the least guessable: it
takes its home from `XDG_CONFIG_HOME`, not from `HOME`, so setting `HOME` does not
move it.
