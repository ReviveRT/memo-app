# Notes

Decisions and trade-offs that the code cannot state for itself.

> **Status: partial.** MEMO-27 owns this file and writes the rest of it. The
> entries below are here early for the same reason: each is a contract that
> several files — and, in every case, two different runtimes — have to agree
> about, so it cannot live in any one of them.

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

## A title is generated by a heuristic, and it is the one column a client may write

**Decision.** `memos.title` is filled by the worker from the memo's own transcript,
by `ai/memo_ai/titles.py` — three regexes and a word list, no model and no key. It is
written on the pass that moves a memo to `ready`, and only when the column is NULL.
`PATCH /api/memos/{id}` accepts a `title`, and that is the only field of a memo's
*content* the API lets a client set.

**Why a heuristic rather than the model that is already running.** The obvious idea is
to have the transcription pass produce the title as a finishing step — one model, one
load, nothing new. It cannot: whisper is speech-to-text and has no summarisation head,
so there is no layer to add. The natural home for a title is a language model, which is
what `ANTHROPIC_API_KEY` and `ENRICH_MODEL` in `.env.example` are for. This module is
what a stack running with neither gets, and the requirement it was written against was
"free, no credits". A second local model would have satisfied the letter of that and
not the spirit — half a gigabyte to download for a five-word label.

**What it can and cannot do, measured rather than claimed.** Four rules, each of which
only ever removes words: a leading run of filler *and date*, a cut at the first clause
boundary, a trailing date, then a six-word cap. "Hello, I would like to leave a note to
book tomorrow tickets on the airplane to Moscow" becomes "Book tomorrow tickets", and
"Remember to call the dentist about the appointment on Thursday morning" becomes "Call
the dentist".

**The date rules are the ones that came from a bug report rather than from a design.**
Without them, "Tomorrow I will have a meeting with my friend John at 15am" titled itself
"Tomorrow I will have" — four function words and no subject — because a spoken memo puts
a date and then a subject, an auxiliary, a light verb and an article in front of the noun
it is about, and the cap ran out before reaching it. Stripping both ends gives "Meeting
with my friend John". Two floors keep those rules honest: a bare weekday is never treated
as a date, because "Sunday Meeting" is a name and one of the best titles this produces;
and a memo that is *only* a date keeps it, because there the date is the content rather
than the frame around it. The clock pattern requires am/pm or a colon, so "the share
price closed at 30" keeps its number.

It has exactly one rule that *names* rather than shortens — three items after `buy`,
`purchase` or `order` is a "Shopping list" — and that vocabulary is three words because
it was six: `get`, `grab` and `pick` each start perfectly ordinary shopping memos *and*
lists of other things, and "Pick up the passport, the birth certificate, the marriage
certificate and the deeds" came back as "Shopping list" until they were removed. What it
cannot do is read eighteen document names and answer "Documents for the job"; that needs
knowing what the items are, and it answers with the first few of them instead.

**The invariant is borrowed from `prose.py` and matters for the same reason.** Every
rule only removes words — from the front, or from the back — and the one exception
replaces the title wholesale rather than editing it. A titler that quietly improved
somebody's wording would be indistinguishable from a transcription error.

**Why the client may write this column and no other.** The transcript is evidence of
what was said: `prose.py` will not add, reorder or respell a word of it, and
`test_the_words_are_never_touched` asserts that over every fixture. A client that could
edit it would remove the property that makes the column worth keeping. A title is the
opposite kind of thing — it is a guess, and a wrong guess on a strip of thirty cards is
a memo the owner cannot find again. So the guess is the default and the owner overrides
it. `status` and `tags` stay out for a different reason: they belong to the queue and
the worker, and a client setting `status` would be a client claiming a job.

**The COALESCE runs the other way from every other clause in that statement.** The
success write is `SET transcript = COALESCE(%(transcript)s, transcript)` throughout —
the *new* value wins, and the row's value is the fallback. `title` is
`COALESCE(title, %(title)s)`: the row wins. That asymmetry is the whole of the
protection. A memo can be re-claimed — a retry, a reaper handing back a lease — and a
worker that recomputed the title on the second pass would overwrite what somebody typed
with what a regular expression guessed, silently, some minutes after they typed it.

**What was rejected.**

- *Generating the title in the API, at insert.* It would work for typed memos and not
  at all for spoken ones, which have no text until the worker is done. Two code paths
  for one column, and the interesting one still in Python.
- *Generating it in the browser for display only.* That is `memoLabel`, which already
  exists and already does it — and it cannot be searched, cannot be edited, and cannot
  reach the collection cards or the reminder notifications, both of which pick a label
  in SQL.
- *A placeholder like "Untitled memo" when nothing is nameable.* NULL, so each reader
  applies its own fallback — the API coalesces to the transcript in SQL, `memoLabel`
  does the same in JavaScript, and both already handle absent. A string written here
  would defeat two working fallbacks to save one null check.

## Deleting a memo writes the row first and the blob second

**Decision.** `DELETE /api/memos/{id}` runs one `DELETE ... RETURNING`, then unlinks the
audio file. Reminders go with the memo through `ON DELETE CASCADE`. If the unlink fails,
the request still succeeds.

**Why that order, given that creating one is the reverse.** `MemoService::createFromAudio`
writes the blob *before* the row, because the worker claims `queued` rows and opens
whatever `audio_path` names — a row that exists before its file is a memo that fails to
transcribe. Deleting has to unwind that: while the row exists, something may still be
about to read the file, so removing the file first produces exactly the failure the
create order was chosen to avoid. Once the row is gone nothing can reach the blob at
all, and what is left is at worst an orphan.

**Why `RETURNING` rather than a SELECT and then a DELETE.** The audio key only exists on
the row being removed, so the unlink needs it — and reading it first is a race: two
clients deleting the same memo would both read the path and both try to unlink, and one
would report a failure for work the other had already done. One statement makes the
row's contents and its removal the same atomic fact, and exactly one caller can win it.
The key is carried the one hop to the service in `DeletedMemo` rather than on `Memo`,
because `audio_path` is a storage key and `Memo` is what every API response is built
from.

**A failed unlink is still a successful delete, deliberately.** The user asked for the
memo to go and it has. Answering 500 would be a server error for a request that already
succeeded, and a client that then shows a memo the database no longer has. Orphan bytes
are reclaimable by a sweep at any later time; a client and a database that disagree are
not. The exception is reported rather than swallowed, so a volume that has stopped
accepting deletes shows up in the log rather than only in `du`.

**Why 200 with the memo rather than 204.** Every other write on this resource answers
with the row, and the frontend reconciles one shape by id. `DELETE /api/collections/{id}`
answers 204 and is the odd one out for a reason that does not apply here: a collection's
contents survive it, so the interesting fact about that request is what happened to the
*memos*, and no response can say. Deleting the same memo twice gives 200 and then 404 —
non-idempotent in its status code, on purpose, because the second request is a client
telling us about a memo it believes exists and it should find out that it does not.
