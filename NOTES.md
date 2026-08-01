# Notes

Decisions and trade-offs that the code cannot state for itself.

> **Status: partial.** MEMO-27 owns this file and writes the rest of it. The
> entries below are here early for the same reason: each is a contract that
> several files — and, in every case, two different runtimes — have to agree
> about, so it cannot live in any one of them.

## One job, two commits, and no second status column

**Decision.** A worker job writes to the memo row twice. Transcription success
commits `transcript`, `duration_ms`, `stt_provider`, `stt_model` and
`cost_micro_usd`, and the row stays `processing`. Enrichment finishing — however it
finished — commits `title`, `summary`, `tags`, `category`, `enriched_at` and
`status = 'ready'`. `failed` is reachable only from the first stage, and only once
the attempts are spent.

**Why not one commit.** Because the two stages have opposite requirements and one
commit forces them to share an outcome. The transcript is the memo and must survive
anything; a summary is a convenience and must cost nothing when it fails. With a
single write, an enrichment error either discards a transcript that already
succeeded — on a hosted provider, one that was paid for — or is swallowed and
reported as success. Both are wrong, and no amount of care inside the job fixes it,
because the problem is that one write cannot express two independent results.

**Why not a second status column.** The obvious alternative is `stt_status` and
`enrichment_status` beside `status`, and it was rejected because the information is
already on the row and would then exist twice. `transcript IS NULL` answers "is
transcription owed" exactly, for both kinds of memo — a text memo is inserted with
its transcript already set (MEMO-06) and therefore owes none. Two columns that must
agree with a third and with the data is three ways to be inconsistent; the predicate
cannot disagree with itself. The claim query, the pipeline and the reaper all read
it, and none of them needs to know what stage a memo is in — only what it lacks.

**What the split buys, concretely.** A job killed between the two commits resumes at
the second one, because the re-claim finds the transcript present. Transcription is
never repeated and never re-billed, which is what makes the reaper safe to be
aggressive with: requeueing a half-finished job costs the enrichment and nothing
else. That property is why the reaper can afford a one-hour lease rather than a
conservative one — the worst case is cheap.

**Costs, stated plainly.** A memo is briefly `processing` with a usable transcript
and no title, and the API shows it as unfinished during that window — correct, but
it means "has a transcript" and "is ready" are not the same question, and a client
that wants the first has to ask for the transcript rather than the status. Two
writes per voice memo instead of one. And a row can be in `processing` with a
transcript and no live worker, which is a state no single statement produces and
which only the reaper's third branch resolves — the case that would be easiest to
forget if the reaper were written before the two-commit split rather than with it.

## The retry cap lives in the claim, not in the failure handler

**Decision.** `attempts` is incremented by the `UPDATE ... RETURNING` that claims a
memo. Nothing on the failure path touches it.

**Why.** The memo that most needs a bound is the one that destroys its worker —
ffmpeg wedged on a pathological file, an out-of-memory kill during a model load, a
`docker compose down` mid-job. That memo never reaches a failure handler, so a
counter incremented on the way *out* of a job would never move for it, and it would
be retried for as long as the stack ran. Incrementing in the claim means the count
is committed before any of our code runs, and it survives a `SIGKILL` with nothing
cooperating. This only works because the connection is in autocommit
(`ai/memo_ai/db.py`): with an open transaction the claim would roll back on the kill
and take the count with it, which was reproduced with two psql sessions before the
worker existed.

**The consequence.** Because a killed job leaves the row in `processing` rather than
in a state the claim will pick up, something has to enforce the cap from outside the
job — otherwise an exhausted memo sits there forever. That is the reaper's second
and third branches, and it is why the cap is checked there rather than in the claim
predicate: a row the claim silently skipped would be an invisible dead end with
nothing on it to explain why nothing is happening.

## The reaper's lease is derived from the deadlines, not chosen

**Decision.** `REAP_AFTER_SECONDS` defaults to 3,600, and
`pipeline.job_budget_seconds` computes the number it has to exceed: 30s of ffprobe
on the upload, 120s of ffmpeg, 30s of ffprobe on the result, 300s of model load, and
a decode deadline of four times the audio — 2,880s at `MAX_AUDIO_SECONDS=600`. The
worker recomputes it at boot and warns if the configured lease no longer clears it.

**Why a check and not a comment.** A lease under the budget does not fail; it reaps
healthy jobs. That presents as transcription being unreliable on exactly the
recordings that take longest, and the row it leaves says it was "interrupted" with
nothing to say by what — a diagnosis nobody reaches from the symptom. And the budget
is not a constant: it scales with `MAX_AUDIO_SECONDS`, so a lease that was correct
when it was chosen is invalidated by an unrelated edit to a different variable. A
comment cannot notice that; a line in the boot log can.

**Why a warning and not a refusal.** The stack still works with a short lease. Memos
are retried rather than lost, precisely because the transcript commit means a reaped
job resumes instead of restarting. Refusing to boot over a tuning number would take
the whole queue down, including text memos, which come nowhere near any of these
deadlines.

## The date filter is half-open, and the browser owns the timezone

**Decision.** `GET /api/memos` and `GET /api/collections` take `from` and `to` as
ISO 8601 **instants**, and the interval is **half-open**: `created_at >= from AND
created_at < to`. The browser converts a chosen calendar day into that pair; the
API has no timezone setting and no `tz` parameter. The two halves live in
`web/src/composables/useDateRange.js` and `api/app/Support/TimeWindow.php`.

**Why the timezone belongs to the browser.** "Yesterday" is a local question — the
same instant is Sunday in Auckland and Saturday in Los Angeles — so only the client
knows which 24 hours were meant. Giving the API a `tz` parameter would mean two
places that both believe they know the user's zone, and the failure when they
disagree is a filter that is silently off by hours. Sending instants leaves exactly
one place that can be wrong.

**Why half-open rather than an inclusive `to`.** The obvious alternative is to end
the range at `23:59:59` on the last day. It drops every row written in the last
second of the range — and `created_at` is a `timestamptz` with microsecond
precision and the wire format carries milliseconds, so it really drops the last 999
ms as well. Nothing surfaces: the list is simply short, at the boundary, for the
newest rows, which is the hardest kind of gap to notice because the newest rows are
the ones being looked at.

The cost is that the client has to add a day, and that is a real trap: a range
ending on the 23rd must be sent as `to = 24th 00:00`. Getting it wrong excludes the
whole last day. Two things guard it — `useDateRange` adds the day in one place and
subtracts it back for the caption, so the `+1` never reaches the screen; and the API
**refuses** `to <= from` with a 422 rather than answering an empty list, because an
empty list is exactly how that bug hides.

**What the filter does not bend for.** The in-flight pin — the rule that a memo
still being transcribed stays in a filtered list regardless of match — is scoped
*inside* the text predicate, so it cannot escape the date window or the collection
scope. Without that, a memo recorded ten seconds ago would appear in a list filtered
to yesterday, and an unfiled memo would appear inside every collection. Both read as
the filter being broken. Verified against a live Postgres: a queued memo is returned
for `?q=nomatch`, returned for `?q=nomatch&collection=<its own>`, and **not**
returned for `?q=nomatch&collection=<another>` or for a window it falls outside.

**What was rejected.**

- *`?from=2026-07-19&to=2026-07-23` as calendar dates.* Reads better in a hand-written
  `curl`, and needs the API to decide what timezone a bare date is in — which is the
  variable this design removes.
- *An inclusive `to`.* Above.
- *Clamping an inverted range instead of refusing it.* A 422 names the problem; an
  empty list is indistinguishable from having no memos.

## Reminders are stored in Postgres and delivered by an open tab

**Decision.** A reminder is a row in `reminders` with an absolute `remind_at`. The
browser polls `GET /api/reminders` for what is still owed, schedules a timer against
the soonest, shows it, and `PATCH`es it delivered. There is no service worker, no
Web Push, and nothing server-side that fires anything.

**What that promises, and what it does not.** With the app open in a tab, a reminder
arrives on time. With it closed, nothing arrives until it is opened again. That
limit is stated on the card itself rather than only here, because a user setting a
7am alarm and shutting the laptop would otherwise reasonably expect it to go off.

**Why the delivery time lives on the server.** `delivered_at` is what makes a
reminder fire once rather than once per page load, and it is a column rather than
browser state because the reminder is not browser state — two tabs, or a reload
mid-delivery, would each fire it again. The `UPDATE` is
`SET delivered_at = coalesce(delivered_at, now())`, which makes acknowledging
idempotent: a retry after a dropped response keeps the *first* delivery time, where
`SET delivered_at = now()` would rewrite it. Verified against a live Postgres — two
PATCHes to the same reminder answered the same timestamp. The alternative guard,
`WHERE delivered_at IS NULL`, is worse in a visible way: the second acknowledgement
matches no row and comes back a 404, telling the client a reminder it is looking at
does not exist.

**Why `now()` and not a timestamp from the client.** This column is the only thing
that can answer "did reminders arrive on time?", and a browser clock is not
something to write it from.

**Why there is a `GET /api/reminders` at all,** when every other reminder route
answers with the memo: the delivery loop has to know about reminders on memos that
are nowhere on screen. The fast strip holds only unfiled memos, so a reminder set
and then filed into a collection would silently never fire. It is a small flat row
per reminder — id, memo id, an 80-character label, the time, the note — rather than
a memo, because a notification body shows about that much and the transcript is the
largest thing on the row.

**Two delivery paths, deliberately.** Due while the app is open gets a system
notification plus an in-app card; due while it was closed gets the card only. A
single "always notify" rule turns a weekend away into eleven system notifications at
once. Both paths acknowledge, so nothing is shown twice.

**Permission is requested when the first reminder is set,** never on load. An
unprompted request has no user gesture behind it, which Chrome and Firefox suppress
or auto-deny — and a denial is permanent, so a badly timed ask does not merely fail,
it removes the option. Refusal costs the system notification and nothing else.

**What was rejected.**

- *`remind_at` and `note` as columns on `memos`.* The card offers an alarm *and* a
  timer, and they are not alternatives — setting one must not silently clear the
  other. A table costs a correlated subquery on the memo projection, which is paid
  because the list has to badge a memo that has something pending.
- *A `delivered` boolean.* A reminder shown four hours late and one shown on time are
  both "delivered", and the difference is the whole of what anyone would complain
  about.
- *A scheduler in the `ai-worker`, or Web Push.* Either would deliver with the app
  closed, and both are a different feature: key management, a push endpoint, and a
  second thing that owes the user something on a timer. The honest small version
  ships first and says what it does.
- *Filtering the pending list to `remind_at <= now()`.* The browser needs the ones
  that have *not* fired yet — that is what a timer is scheduled against. Filtering to
  what is already due would leave the client polling to discover the future.

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
