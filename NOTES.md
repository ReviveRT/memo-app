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

**One hand-off, for whoever builds the manual retry (MEMO-17).** Nothing resets
`attempts`, deliberately — it is a record of what the memo cost, and MEMO-22 reads
that kind of column. But it means moving a `failed` row back to `queued` and
nothing else buys exactly one more attempt, because the claim increments a count
that is already at the cap and the next failure is terminal again. A manual retry
is a person saying "the reason it failed is gone", so it should set
`attempts = 0` along with the status. That is a one-line difference between a retry
button that works and one that looks like it does nothing.

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

## Model weights are fetched at build time, and the cache volume is the exception

**Decision.** `ai/Dockerfile` downloads all three model weights during
`docker compose build`, at pinned revisions, and bakes them into the image at
`/opt/models`. Nothing is fetched at runtime on the shipped configuration. The
`model-cache` volume, mounted at `/cache`, is still there but now holds nothing
unless somebody sets `STT_MODEL` to a size the image does not carry.

The weights are 2.81 GB, which takes the unpacked image from 0.95 GB to 3.77 GB.
`docker images` reports 1.27 GB → 6.74 GB for the same two builds, because on
Docker's containerd store that column adds the compressed layers kept beside the
snapshot. Both numbers are real and they are not the same measurement; README.md
quotes the `docker images` one, since that is the number a reader will see.

**Why not at first use.** There is no paid API anywhere in this project, so the
local models are not an implementation detail of the product — they *are* the
product, and "works with no account and no key" is a claim about them. A model
fetched lazily makes that claim conditional on the reviewer's connection: on a slow
link the first recording waits out `MODEL_LOAD_TIMEOUT_SECONDS` and fails, and with
networking off it fails immediately. Both are the same failure — a stack that
appeared to converge and then did not work — and it lands on the first thing anyone
tries.

**Why three models and not one.** The two obvious ones are `large-v3-turbo` for
transcription and Qwen2.5-1.5B-Instruct for enrichment. The third is `tiny`, and it
is the one that gets missed: `STT_LANGUAGE` ships empty, meaning detect the language
per recording, and `memo_ai/stt/local.py` runs that pass on `tiny` rather than
paying a second encoder pass on turbo. So the shipped configuration loads two
whisper models on the first voice memo. An image that baked only the big one would
still open a socket, for 78 MB, and fail the offline criterion on a detail nobody
would look for.

**Why the weights are not under the volume mount.** The obvious arrangement — bake
into `/cache` and let the `model-cache` volume sit on top — looks like it works,
because Docker seeds an empty named volume from its mount point and the first `up`
would copy the weights out of the image. It is a trap. From then on the volume is
authoritative and the image is not: rebuilding with a different `STT_MODEL` changes
nothing anyone can observe until they run `docker compose down -v`, and the symptom
is a stack that keeps using a model you can prove is no longer in the image. Two
directories instead — `/opt/models` immutable and root-owned, `/cache` writable and
owned by `memo` — is what keeps a rebuild meaningful.

**Why the enrichment weights are baked before there is an enricher.** MEMO-21 writes
the local LLM pass; today `memo_ai/enrich.py` is a contract and a null
implementation, so the 1,117 MB GGUF at `ENRICH_MODEL_PATH` is read by nothing. It
is baked now because this is the task that owns "no surprise runtime download", and
deferring it means a second 1.1 GB layer landing later on everyone who already holds
a built image. The cost of being early is one layer nobody uses yet; the cost of
being late is paid by every existing checkout.

**What this does not buy: speed.** Both models run on CPU and there is no
configuration of this stack in which they do not. CTranslate2, which faster-whisper
runs on, has no Metal backend at all, and llama.cpp inside a Linux container gets no
GPU passthrough on macOS — so on Apple silicon the host's GPU is unreachable from
both halves regardless of what Docker is told. Whisper runs at `int8` for that
reason. Transcription is slower than realtime and the README's speed table is
measured, not estimated.

**The image must be arm64-native.** This follows from the paragraph above rather
than being a separate preference: with everything on CPU, an amd64 image running
under Rosetta emulation on an Apple silicon Mac is not "somewhat slower", it is
unusable for a 1.5B model and a large whisper encoder. `docker compose build` on an
arm64 host produces an arm64 image by itself and needs no flag. What produces the
broken case is asking for the other one — `--platform linux/amd64`, or pulling a
prebuilt image from a registry that only published amd64. Do not.

## A title is generated by a heuristic, and the owner overrides it

**Decision.** `memos.title` is filled at commit point 2 by a COALESCE over four
sources, in this order: whatever an enricher produced, whatever is already on the
row, a short phrase `ai/memo_ai/titles.py` cuts out of the transcript, and finally
the SQL expression that takes its first sixty characters. `PATCH /api/memos/{id}`
accepts a `title`, and also a `transcript` — the two fields of a memo's *content* the
API lets a client set.

**Why two fallbacks rather than one.** They answer the same question with different
information, and the difference is where the transcript is. `titles.py` runs in
Python and needs the text in memory; the SQL expression needs only the row. Almost
always the first is available and is much the better answer — "Meeting with my friend
John" against "Tomorrow I will have a meeting with my friend John at 15a…" — so it
goes first. But the reaper's salvage branch publishes rows in bulk with no job in
memory at all, so the SQL one stays as the last resort rather than being replaced.
`_REAP_SALVAGE` uses it alone, and that is the case it exists for.

The pipeline hands the text to commit 2 explicitly rather than letting the statement
read `memo.transcript`, because at that point a *fresh* voice memo's transcript is on
the row and not on the claim — the claim happened before the transcript existed. It
is the same expression the enricher is given, so a memo's title and its summary can
never turn out to be about different text.

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

**Why the client may write the two content columns and no others.** Both are guesses,
and a guess the owner disagrees with is worth being able to overrule. A title is a
guess about what a memo should be called, and a wrong one on a strip of thirty cards is
a memo they cannot find again. A transcript is a guess about what was said, and a wrong
one is a memo that no longer says it.

This section previously argued the opposite for the transcript — that it was *evidence*
of what was said, that `prose.py` will not add, reorder or respell a word of it (which
`test_the_words_are_never_touched` still asserts over every fixture), and that a client
able to edit it would remove the property making the column worth keeping. That argument
did not survive contact with a wrong transcript. The evidence is the **recording**, which
is kept on the volume and served back; the transcript is a lossy derivative of it produced
by a model, and calling a guess a record does not make it one. The property being
protected was also not real: a memo transcribed in the wrong language is not evidence of
anything, and refusing the owner a correction protected nothing while leaving them no way
to fix it. The formatter's invariant is untouched by the change — *it* still never
rewrites a word. What changed is that a person may.

The case that settled it is recorded under the language picker below: a Romanian memo came
back transliterated into Cyrillic, and nine language-ID approaches across three model
architectures all misidentified the same clip. Re-running the model was built first and
then removed — it is slow, it discards a transcript that may be mostly right, and it is at
the mercy of the same detection that failed. Typing the correction always works.

`status` and `tags` stay out for a different reason, unchanged: they belong to the queue
and the worker, and a client setting `status` would be a client claiming a job.

**The row's own title sits ahead of both fallbacks, and that is what makes the column
safe to edit.** A memo can be re-claimed — a retry, a reaper handing back a lease —
and a worker that recomputed the title on the second pass would overwrite what
somebody typed with what a regular expression guessed, silently, minutes after they
typed it. Only an *enricher's* title outranks it, which is the one case where
something read the whole transcript rather than its first clause.

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

## Retry is guarded on `status = 'failed'` inside the UPDATE, and answers 409 otherwise

**Decision.** `POST /api/memos/{id}/retry` is one statement — `UPDATE memos SET status =
'queued', attempts = 0, next_attempt_at = now(), locked_at = NULL WHERE id = ? AND status
= 'failed' RETURNING ...`. A row comes back and it is a 200; no row comes back and a second
read decides between 404 (no such memo) and **409** (there is one, and it is not failed).

**The status predicate is safety, not a tidy way to reach a 404.** Two of the four states
are actively dangerous to requeue:

- `processing` means a live claim. The worker's writes are fenced on `locked_at`, not on
  the status, so a row put back to `queued` underneath its owner is claimable by the other
  replica while the first is still transcribing it — two workers, one recording, and on a
  hosted provider two bills. The fence protects the *writes*; nothing protects the claim
  predicate from a third party moving the status out from under it.
- `ready` means finished. Requeueing re-runs enrichment for nothing and overwrites `title`
  — which may be the one the owner typed, since renaming is the one content write a client
  has.

Doing the check in the `WHERE` rather than in a `SELECT` in front of the `UPDATE` is what
makes it hold: a check and a write in two statements is a window, and this is precisely a
route two tabs will press at once.

**Why `attempts = 0` is load-bearing rather than tidy.** A failed memo sits at the cap, and
the claim increments *before* any worker code runs (that is what makes the count survive a
`SIGKILL`). Requeued at `attempts = MAX_ATTEMPTS`, a memo would get exactly one more go:
`fail_or_retry` reads it as already exhausted, so any failure is immediately terminal with
no backoff, and the reaper's `attempts < max_attempts` requeue never matches it either.
Zero gives it the budget a new memo has, which is what a person pressing Retry means.
`next_attempt_at = now()` is the same argument from the other side — the column may hold a
backoff from the attempt that failed, and a press that produced no visible change for
thirty seconds reads as a button that does not work.

**`last_error` is deliberately left where it is.** It is the *last* error, not the current
state; the worker's own retry path writes it onto a `queued` row for exactly that reason.
The frontend gates the sentence on `status === 'failed'`, so a requeued memo stops showing
it without the column being touched, and the next successful transcription clears it —
which is the write that knows the error is over.

**Why 409 and not a 404 or a cheerful 200.** This is the one route in the project that
answers 409, and the argument is what the client does next. Everywhere else, "you named
something that is not there" collapses into one 404 because the remedy is the same; here
the second case is a memo that is right there and has simply moved on — the worker finished
it, or the other tab pressed first. The frontend renders these sentences verbatim, so a 404
would put "That memo no longer exists" under a memo the user is looking at. A 200 was the
other candidate — retry-as-idempotent — and it would report a refusal as a success, which
hides the `ready` case rather than describing it.

**A 5xx body is never rendered, which is the same rule one layer out.** `request()` replaces
the body of any 500 with a sentence naming the log: with `APP_DEBUG=false` Laravel answers
`{"message":"Server Error"}`, and with it on the same body carries the exception, the file,
the line and a trace — so passing `message` through would put a DSN in a memo card the day
somebody debugged something. 4xx messages stay verbatim, because those are written next to
the rule that rejected the request and are the whole reason the client reads `message` at
all. It is the rule `memo_ai/audio.py` applies to ffmpeg's stderr and `pipeline.py` to an
unclassified exception, applied to the API itself: the detail goes to the log, and the user
gets a sentence this project wrote.

## A failed memo carries a code as well as a sentence, and empty ones are deleted

**Decision.** `memos.last_error_code` holds a short token from a closed vocabulary
(`ai/memo_ai/failures.py`) written by the same statement that writes `last_error`. Two of
its values — `no_speech` and `no_audio` — mean the recording had nothing in it, and the
frontend deletes those memos rather than rendering them, explaining itself in a toast.
Every other failure keeps its card, its reason and its Retry button.

**Why a card for an empty recording is worse than no card.** A memo whose entire content
is "you did not say anything" is not a memo. It cannot be transcribed now, no retry will
find words that were never spoken, and it sits in the strip beside real memos until
somebody deletes it by hand — so the list slowly becomes a list of the user's misfires. The
information is worth exactly one sentence, once, at the moment it happens, which is what
the toast is.

**Why the classification is a token and not the sentence.** `last_error` is prose, worded
where the fault was detected so that the module that knows there are three causes of
silence can say so. Prose gets reworded. A frontend keyed to a substring of it breaks
*silently*, and both directions are bad: memos quietly stop being tidied up, or the wrong
ones start being deleted — and the second one takes the recording with it. The code and the
sentence both come from the raise site, so neither is derived from the other and they
cannot disagree.

Deliberately no CHECK constraint on the column, unlike `source` and `status` beside it.
Those are the memo's lifecycle and a new value should cost a migration; this is a
diagnosis, and a provider added later should be able to name a new way of failing without
one. An unrecognised code keeps the memo, so an unknown value is never destructive.

**Why the browser deletes it and not the worker.** The worker knows first and could delete
the row itself — that was the first design, and it is wrong for one reason: the user has to
be told. Deleting server-side leaves the browser's toast stuck on "Transcribing…" and a
recording that vanished without a word, which is the silent gap MEMO-17 exists to close.
Doing it in the browser makes the removal and the explanation one event, in the one runtime
with a screen. Ordered accordingly: the toast is raised first, because the reason exists
only on the row that is about to go; the row leaves the list only once the API has
confirmed the delete, which is the same rule the create and delete paths already follow.
The cost is that a memo failing while no tab is open is not tidied until a tab next sees it
— a delay rather than a hole, and until then it is an ordinary failed card.

**What was rejected.** Refusing silent recordings in the browser before upload, using the
existing voice-energy meter: the fastest possible feedback, and it discards a genuinely
quiet memo that the meter misjudges before any row exists to recover it. And discarding
*every* failure, which is simpler and matches the obvious reading of "do not create a memo
if it failed" — it throws away recordings for faults that had nothing to do with what was
said, such as a model that had not finished downloading, and it makes the Retry button
unreachable.

## Playback ranges are answered by PHP, not by Caddy — with the accelerated path one line away

**Decision.** `GET /api/memos/{id}/audio` returns a Symfony `BinaryFileResponse` — through a
one-method subclass, see the end of this section — which parses `Range`, answers `206` with
`Content-Range`, `416` for a range past the end, and advertises `Accept-Ranges: bytes`.
Nothing in this repo parses a range header. Single ranges only, which is every range a media
element sends; a multi-range header is answered with its first range rather than a
`multipart/byteranges` body. The
`X-Accel-Redirect` path that would hand the bytes to Caddy is not enabled, and the reasons
are below because "let the web server do it" is the conventional advice and this goes the
other way.

**The premise that FrankenPHP has an equivalent of nginx's `X-Accel-Redirect` does not
hold, and it was checked rather than assumed.** The string does not appear in the shipped
binary (`grep -a X-Accel-Redirect /usr/local/bin/frankenphp` on `frankenphp v1.12.6`). What
exists is a generic Caddy recipe: `http.handlers.intercept` *is* in this build's module
list, so a `intercept` block matching the header and handing off to `file_server` would
work. Symfony would emit the header from this same response object — `BinaryFileResponse`
has `X-Sendfile`/`X-Accel-Redirect` support built in — so the controller would not change
at all. Three things made it the wrong default here:

- **The config has nowhere good to live.** This image ships no Caddyfile of its own; it
  uses the base image's, whose only in-site extension point is the `CADDY_SERVER_EXTRA_DIRECTIVES`
  environment variable. That means a multi-line Caddyfile snippet inside `docker-compose.yml`,
  where a typo does not degrade playback — it stops Caddy from starting, and takes the whole
  API with it. `docker compose up` converging on a clean checkout is the property this
  project is least willing to trade.
- **No test in this suite could reach the bytes.** The feature suite runs the controller in
  a PHP process with no Caddy in front of it, so the accelerated path is only ever assertable
  as "the right header was set". As it stands, `MemoAudioEndpointTest` asserts the actual
  bytes a range produced against the same slice of the file, which is the part that can
  silently go wrong.
- **Turning it on is a footgun in its own right.** `BinaryFileResponse::trustXSendfileTypeHeader()`
  trusts `X-Sendfile-Type` from the **request**. With it enabled and no Caddy block to
  inject and strip that header, any client could send it and get back an empty `200`
  carrying the absolute path of the file on the volume.

**What makes PHP an acceptable place for these bytes.** A recording is capped at 12 MiB
(`MAX_AUDIO_BYTES`), so no response is large. `BinaryFileResponse` seeks and copies in
chunks rather than reading the file into memory. And the api container already runs a
threaded server chosen for exactly this request: `api/Dockerfile` says FrankenPHP rather
than `php -S` because the built-in server is single-threaded and one client holding a
streaming audio response would stall every status poll behind it. The decision to make this
survivable was taken before there was anything to serve.

**How to switch, if a deployment ever wants to.** Add an `intercept` block to
`CADDY_SERVER_EXTRA_DIRECTIVES` rooted at `AUDIO_DIR`, and call
`BinaryFileResponse::trustXSendfileTypeHeader()` in `AppServiceProvider::boot()`. Both
halves are required and neither is safe alone.

**The frontend has to force a duration out of the file, and that is not a bug in this
endpoint.** A WebM from `MediaRecorder` carries no Duration element — `config/memo.php`
records the same measurement from the upload side, and it is why the worker measures length
with `ffprobe` after normalization. So `audio.duration` is `Infinity` in Chrome and Edge, a
native player has nothing to size its scrubber from, and seeking does not work no matter how
correct the server is. Measured on this stack before the workaround: `duration: Infinity`,
`readyState: 4`, playback fine, `currentTime` unsettable. `MemoDialog` seeks once past the
end of the file on `loadedmetadata`, which makes the element discover the real length
(`4.08` for that memo), then resets to zero. Re-muxing every recording so the container
declares what it already contains was the alternative: work in the worker, a second copy of
every blob, and a normalization step that exists for the transcriber rather than for the
player. What the workaround costs was measured rather than assumed: on the recordings on this
stack, nothing at all — `preload="metadata"` has already pulled the whole file in one `206`,
so the seek to the end is served from the buffer and no second request is made. A recording
too large to buffer whole asks for the tail instead, which is one range request rather than a
second download of the entire file. That upper bound is this endpoint's doing.

**The framework's range handling had one thing wrong, and the suite is what found it.**
Symfony sets `Content-Length` to the size of the whole file before it reads the `Range`
header and overwrites it only on the `206` path. An unsatisfiable range takes the other
branch: the status becomes `416`, `sendContent()` writes nothing because the response is no
longer successful, and the header still promises the entire file. Against the running
container on `symfony/http-foundation v7.4.15`, a `Range: bytes=999999-1000000` answered
`416` with `Content-Length: 24775` and zero bytes of body — and curl reported `transfer
closed with 24775 bytes remaining to read` rather than a clean refusal, with the connection
unusable afterwards. Caddy does not correct it; the promise goes out as written.
`App\Http\Responses\AudioFileResponse` overrides `prepare()` to set `Content-Length: 0` on
that status alone — the controller cannot, because `prepare()` runs after the action returns
and is the code that introduces the problem. `HEAD` is deliberately left alone: it also sends
no body, and there the full length is exactly what the header should say.

Worth recording because of how it hid. The first version of the 416 test asserted the status
and the `Content-Range` and passed, which is the shape of assertion that reads as coverage
and is not: nothing in it was about the bytes. The test now pins the framing as well.
