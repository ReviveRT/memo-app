# Deploying on a free tier

This directory holds the production build: **one container** serving the built Vue app, the
API, a transcription worker and Ask from one origin, plus the two things a hosted deployment
needs that compose does not — object storage for recordings, and a retention job.

`docker-compose.yml` at the repository root is unchanged and is still the way to run this
locally. Nothing here replaces it.

```bash
docker build -f deploy/Dockerfile -t memo-app .
```

Build from the repository root, not from `deploy/` — the image needs `api/`, `web/`, `db/`
and `ai/memo_ai/` together.

## The short version

Push this repository to GitHub, then point Render at it:

<https://dashboard.render.com/select-repo?type=blueprint>

`render.yaml` at the repository root describes the web service and a Postgres, so the only
thing to type is a Groq API key — and even that is optional on the first pass. Everything
below is either the reasoning behind that file or the equivalent for another platform.

Two things that look like failures and are not. **The blueprint creates the database and the
web service together and does not promise an order**, so the first deploy can health-check
before Postgres is ready; that attempt fails and the retry succeeds. The container is built to
survive it rather than crash-loop — with no `DATABASE_URL` it serves the SPA, answers
`/api/health` with 503 and starts no worker. And **the first request after 15 minutes idle
takes about a minute**, because free services sleep.

## Why one container

The memos on a page belong to a browser, not to an account: the API sets a long-lived
`HttpOnly` cookie and every query is scoped by it. A cookie belongs to a *host*, so the
moment the SPA is served from one origin and the API from another, `SameSite=Lax` stops
sending it on XHR — and recovering from that means `SameSite=None`, a CORS layer, and a
CSRF token to replace the protection `Lax` was providing. Serving both from one Caddy
config costs nothing and avoids all three.

It also happens to be the shape a free tier gives you: one web service.

## What you need

| Thing | Free option | Notes |
| --- | --- | --- |
| Postgres 16 | Neon, Supabase, Render | `pgcrypto` is used by one migration; see below if your provider refuses `CREATE EXTENSION`. Render's free database **expires after 30 days**; Neon's does not |
| A web service | Render, Railway, Fly.io, Koyeb | 512 MB is comfortable — see the measurement below |
| Transcription | Groq | Free key, no card. Optional — see below for what a deployment without one does |
| Object storage | Cloudflare R2 | 10 GB, and **no egress charge** — which for audio is the whole bill. Optional; without it recordings do not survive a redeploy |

Only the first two are required.

## Environment

Required:

```
DATABASE_URL=postgres://user:pass@host:5432/dbname
APP_ENV=production
APP_DEBUG=false
```

`APP_DEBUG` matters more than it looks. With it on, Laravel's 500 body carries the
exception, the file, the line and a full trace — and the frontend renders a 4xx `message`
verbatim into the UI.

**Port.** Caddy binds `SERVER_NAME`, defaulting to `:8080`. Platforms that assign a port
inject it as `$PORT`, which Caddy cannot substitute into another variable's default, so set
`SERVER_NAME` directly:

| Platform | Set |
| --- | --- |
| Render | `SERVER_NAME=":10000"` |
| Railway | `SERVER_NAME=":8080"` and let it detect, or match `$PORT` |
| Fly.io | `SERVER_NAME=":8080"`, `internal_port = 8080` in `fly.toml` |

**Recordings.** Without these, audio is written to the container's disk and is gone on the
next deploy — transcripts survive in Postgres, the recordings do not.

```
AUDIO_BUCKET=memo-audio
AUDIO_BUCKET_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
AUDIO_BUCKET_KEY=<R2 access key id>
AUDIO_BUCKET_SECRET=<R2 secret access key>
AUDIO_BUCKET_REGION=auto
AUDIO_BUCKET_PATH_STYLE=true
```

Create the bucket **private**. Nothing needs public access: the API mints a short-lived
signed URL per playback, after checking the memo belongs to the browser asking. A
public-read bucket would make every recording readable by memo id alone, which is exactly
what the owner cookie exists to prevent.

Naming a bucket without the endpoint or the credentials is refused at startup rather than
tolerated, so a typo is one clear error instead of memos failing one at a time.

## Migrations

`deploy/entrypoint.sh` runs `db/migrate.sh` before starting the web server, and the server
only starts if it succeeded. There is nothing to wire up.

Two things worth knowing:

- The migration that adds owners uses `pgcrypto`, but **only in its backfill**, which runs
  only when the database already contains memos — never on a fresh deployment. If your
  provider refuses `CREATE EXTENSION`, that one line is what to cut. The running
  application needs no extension.
- If you deploy this over a database that already has memos, the migration assigns them all
  to one owner and prints a claim link in the boot log. Open it once and they are yours.
  It is printed there and nowhere else — only the hash is stored.

## Retention

Anonymous identities cost nothing to create, so `owners` grows with traffic rather than
with users. On a free Postgres capped near half a gigabyte, that is what eventually fills
it.

```bash
php artisan memo:prune-owners --dry-run
php artisan memo:prune-owners
```

It deletes owners not seen within `OWNER_COOKIE_DAYS`, their memos, collections and
reminders (by cascade), and their recordings from whichever storage is configured — which
is why it is a command and not a SQL script: psql cannot delete from a bucket.

Run it monthly. Nothing in this repository schedules it, because platforms disagree too
much about how a periodic job is expressed: Render has Cron Jobs, Railway has cron, Fly has
`fly machine run`. Any of them, with the command above.

Half of the problem is already handled without this: a cookie-less `GET` is answered from an
empty transient owner and writes no row, so crawlers and uptime pingers leave nothing
behind. That matters because a free deployment has an uptime pinger by construction — it is
how you stop the instance sleeping — and at one ping a minute an eager design would create
1,440 rows a day.

## The worker

**It runs inside this container**, and the reason is worth reading before deciding to move
it out.

An earlier version of this file claimed the application degraded gracefully without any
worker — "typed memos work completely, voice memos stay `queued`". That was measured and it
is wrong. A typed memo is written `queued` too, and it is the worker that moves it to
`ready`. With no worker at all, *nothing* on the page ever leaves "Waiting for a worker…",
and the frontend polls it forever. There is no useful deployment of this application without
a worker somewhere.

The obvious place is a second service, and free tiers are exactly where that is hardest:
Render bills background workers from the first one. So `deploy/Dockerfile` installs the
worker beside the API and `deploy/entrypoint.sh` starts both. What makes that reasonable
rather than a hack is the cost once the models live somewhere else — **41 MB RSS**, measured
on the shipped configuration, against the ~2.4 GB per replica a local Whisper model needs.
It is network-bound on Groq and idle the rest of the time.

The image sets the three variables this needs:

```
STT_PROVIDER=groq
STT_FALLBACK=groq
ENRICH_PROVIDER=none
```

`STT_FALLBACK` is the one that would be forgotten. Left at its default of `local`, every Groq
failure would fall through to a model this image does not contain and the memo's recorded
error would name the wrong provider.

`ENRICH_PROVIDER=none` costs the titles, summaries, tags and categories a local LLM writes;
there is no hosted enricher to point at instead, because that seam exists for speech-to-text
only. Memos still get a title — `memo_ai/titles.py` falls back to the first line of the
transcript — so the list stays readable rather than becoming a wall of "Untitled".

### Without a Groq key

A supported configuration, and the one a first deploy lands in:

- **typed memos work completely** — transcript, title, collections, reminders, search;
- **a voice memo fails in a few seconds**, with `GROQ_API_KEY is not set` on the card, rather
  than hanging;
- **Ask refuses** with `Ask is not configured: GROQ_API_KEY is not set on this deployment`.

Ask used to be listed here as the one feature a free tier could not host "at any price",
because `ai-api` loaded a 1.1 GB local model. That was a claim about the *model*, not about
the feature, and it stopped being true when `ASK_PROVIDER=groq` arrived — the same key that
transcribes now answers questions too. See the Ask section below.

One wrinkle in that voice-memo error, because it will be read and acted on: it ends with "or
set `STT_PROVIDER=local` to transcribe on this machine", which is sound advice under compose
and impossible here — this image has no weights, and pointing it at `local` trades a clear
error for a confusing one. The sentence lives in `memo_ai/stt/groq.py`, where it is correct for
the image that file was written for. Set the key instead.

### A memo caught by a restart

Free tiers restart a container often — every deploy, and every wake from sleep. A memo that was
mid-flight when that happens is left `processing`, and the reaper takes it back only after
`REAP_AFTER_SECONDS`, which is **3600**. Worst case, one memo shows "Transcribing…" for an hour.

That number is deliberately not lowered here. It is derived from the slowest job the *local*
provider could legitimately run — `memo_ai/pipeline.py`'s `job_budget_seconds`, which the worker
recomputes at boot — and on Groq the real bound is a fraction of it. Setting it to 600 was tried:
the worker starts and warns on every boot that "the reaper will requeue jobs that are still
running", because the budget it checks against is 2880s at `MAX_AUDIO_SECONDS=600`. A permanent
boot warning that looks like a misconfiguration is worse than the exposure.

The lever, if an hour ever matters: lower `MAX_AUDIO_SECONDS` first — that shrinks the budget by
four seconds for every second removed — and only then lower the lease to match. Capping a demo
at two minutes of audio makes a 1200s lease legitimate and warning-free. The window itself is
about two seconds wide per memo on Groq, which is why this is documented rather than fixed.

### If the worker refuses to start

The container exits and takes the web server with it, so the platform shows a crash loop with
the reason on the last line of the log rather than a site that looks fine and quietly never
finishes a memo. Verified with `AUDIO_BUCKET` set and its three companions missing: exit
code 2, and

```
ai-worker: AUDIO_BUCKET is set, so AUDIO_BUCKET_ENDPOINT, AUDIO_BUCKET_KEY,
AUDIO_BUCKET_SECRET must be set too.
```

A missing `GROQ_API_KEY` is deliberately *not* one of these — the worker starts and fails
individual voice memos, because a key that expires should not take the whole site down. If you
need the site up while a worker problem is being sorted out, set `RUN_WORKER=false`.

### Running it as a separate service instead

Set `RUN_WORKER=false` on the web service, then run the same image somewhere else with the
entrypoint pointed at the venv:

```bash
docker run --entrypoint /opt/memo-ai/venv/bin/python \
  -e PYTHONPATH=/opt/memo-ai \
  -e DATABASE_URL=... -e GROQ_API_KEY=... \
  memo-app -m memo_ai.worker
```

Both must see the same `DATABASE_URL` and the same bucket. Running two workers against one
queue is safe — claims are atomic — but on a demo it doubles the polling for no gain.

## Ask

**It works here**, answered by Groq rather than by a model in this container, and it runs as a
third process beside the API and the worker. The image sets:

```
ASK_PROVIDER=groq
AI_API_URL=http://127.0.0.1:8000
```

`AI_API_URL` is the line that would be forgotten. Its default is `http://ai-api:8000` — a
compose service name that resolves to nothing here — so without it every question fails on DNS
and the widget reports the service as not running.

Ask needs no key of its own. It reads the same `GROQ_API_KEY` transcription uses, so a
deployment that can transcribe can already answer questions, and one without a key refuses both
with a sentence naming the variable.

### Why this is a different model from the local one

`GROQ_ASK_MODEL` defaults to `llama-3.1-8b-instant`, against the local backend's
Qwen2.5-1.5B — so the hosted path is the *larger* model, not a downgrade. It is also
dramatically faster: **0.23 s** for a complete cited answer here, against tens of seconds for
the local model on a laptop, because a 1.5B model generating 320 tokens on shared CPU threads
is the slowest thing in this application.

An 8B model rather than one of the 70B ones because the prompt is extractive:
`memo_ai/ask/prompt.py` fences three memos and asks for an answer drawn only from them, with
numbered citations. That is reading comprehension over ~3,600 characters, which an 8B model does
about as well as a much larger one, several times faster, and at a rate limit a free plan does
not exhaust in a demo. Groq's catalogue moves, so this is a variable rather than a constant — if
the default is ever retired, set `GROQ_ASK_MODEL` and redeploy without a rebuild.

### One thing that got better rather than worse

The local backend serialises questions behind a lock, because two generations on four shared
threads make both slow and neither correct. Groq has no such coupling, so **two people can ask
at once** — the only behavioural difference between the backends a user could notice, other than
the answer arriving in about a second.

### Running the local model instead

Set `ASK_PROVIDER=local` and the entrypoint does not start ai-api at all, because this image
bakes no weights for it — it would report `missing` on `/health` forever and refuse every
question. To have the local model, run `ai-api` from the `ai` image somewhere with ~2 GB and
point `AI_API_URL` at it.

## What is not in this image

**The baked models.** `ai/Dockerfile` bakes ~2.8 GB of weights and neither that image nor the
~2.4 GB per replica it needs at runtime will fit in a free tier. `ai-api` itself *is* here now
-- see the Ask section above -- because what did not fit was the 1.1 GB model behind it rather
than the service.
See `ai/requirements-hosted.txt` for what was left out of the Python install and why each is
safe to drop.

### What it costs to run

Measured with the container capped at `--memory 512m`, which is what Render's free tier gives:

| | |
| --- | --- |
| Idle, both processes up | **58 MB** |
| Peak under 300 mixed requests, 30 concurrent | **103 MB** |

All 101 memos in that run reached `ready` and `/api/health` still answered 200 at the peak. So
512 MB is not tight — it is about five times what this needs. An earlier revision of this file
put the API alone near 90 MB, which was a guess and too high for the pair of them together.

The constraint on a free tier is CPU rather than memory: Render's free plan is 0.1 vCPU, and
transcription is Groq's problem rather than this container's, but `ffmpeg` normalizing a
recording is not. A long memo takes noticeably longer to normalize here than it does locally.

### What the worker costs this image

Measured, because the first version of this section guessed and guessed low:

| | |
| --- | --- |
| `psycopg` and the venv around it | 38 MB |
| `memo_ai/` source | 0.6 MB |
| `ffmpeg`, `python3`, `python3-venv` and their dependencies | **422 MB** across 198 packages |

The last row is the whole story, and almost none of it is audio code. Debian's `ffmpeg`
depends on `libavfilter`, which depends on `libplacebo`, which pulls Mesa and with it
`libllvm19` — 118 MB of GPU rasteriser, 33 MB of `mesa-libgallium`, and beside them 27 MB of
`libflite1` (speech *synthesis*) and 26 MB of `libz3-4` (an SMT solver).
`--no-install-recommends` is already on and does not touch any of it; these are hard
dependencies.

The image that comes out is **1.2 GB unpacked and about 390 MB compressed**, which is what a
platform actually pulls. Measured with `du` inside the running container and with
`docker export | gzip` respectively — not from the `docker images` SIZE column, which counts
the unpacked tree *and* the compressed layers beside it and so reads about double.

`ai/Dockerfile` weighed the same choice and kept the distro package, on the explicit grounds
that 120 MB of unused GPU stack was "about four percent of the image rather than a sixth of
it". That arithmetic does not survive the move here: with no weights to dwarf it, the tree is
about a third of this image rather than four percent. The package is kept anyway — a signed
distro package beats opaque binaries from a third-party registry, and 390 MB clears every
free tier's pull limits — but the reasoning it was kept *for* no longer applies, and the lever
is real if a platform's build ever runs out of disk: static `ffmpeg`/`ffprobe` binaries carry
no dependency tree at all, and the worker shells out to exactly those two.

## Troubleshooting

### `Exited with status 126` and `frankenphp: Operation not permitted`

Fixed in the image, and written down because it cost a first deploy and the error names none of
the cause.

```
/usr/local/bin/memo-entrypoint: line 56: /usr/local/bin/frankenphp: Operation not permitted
==> Exited with status 126
```

The `dunglas/frankenphp` base image ends with `setcap cap_net_bind_service=+ep` on its binary so
it can bind :80 unprivileged. When the kernel execs a file whose **effective** capability bit is
set, it requires that capability to be inside the process's bounding set — and hosted runtimes
narrow that set, since they hand the service a high port and have no reason to grant it. The
exec fails with EPERM.

It is a perfect false negative locally: Docker's default capability set *includes*
NET_BIND_SERVICE, so every local test passes and the first hosted deploy dies. Reproduce it
without deploying anything:

```bash
docker run --rm --cap-drop NET_BIND_SERVICE memo-app
```

`deploy/Dockerfile` now runs `setcap -r` on the binary and asserts the attribute is gone, so the
exec depends on nothing the runtime might drop. This image never binds a privileged port — :8080
by default, :10000 on Render — so the capability was dead weight. The failure is not specific to
the two-process entrypoint: it reproduces on the plain `exec "$@"` path too.

### `/api/health` returns 200 but every memo endpoint returns 500

The schema is behind the code — `/api/health` reports database *connectivity*, not whether the
migrations ran. Check what is applied:

```bash
docker compose exec db psql -U memo -d memo -c "select filename from schema_migrations order by filename;"
```

Compare against `ls db/migrations/`. Under compose, `docker compose run --rm migrate` applies the
rest; it is idempotent. Note that `007_owners.sql` prints a **claim link once and never again**
when it backfills existing memos — capture it from that output, or those memos become
unreachable.

## Limits of the owner model

Stated plainly, because the schema cannot enforce what the design gives away:

- **A claim link is a bearer credential.** Whoever has it has those memos. There is no
  second factor and no revocation.
- **A cleared cookie is a lost account** unless the claim link was saved somewhere. There is
  no email to recover through, because there is no email.
- **This is not a security boundary for anything sensitive.** It is the right trade for
  memos on a hobby deployment and the wrong one for anything a person would be harmed by
  losing.
