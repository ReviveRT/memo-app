# Deploying on a free tier

This directory holds the production build: **one container** serving the built Vue app and
the API from one origin, plus the two things a hosted deployment needs that compose does
not — object storage for recordings, and a retention job.

`docker-compose.yml` at the repository root is unchanged and is still the way to run this
locally. Nothing here replaces it.

```bash
docker build -f deploy/Dockerfile -t memo-app .
```

Build from the repository root, not from `deploy/` — the image needs `api/`, `web/` and
`db/` together.

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
| Postgres 16 | Neon, Supabase, Render | `pgcrypto` is used by one migration; see below if your provider refuses `CREATE EXTENSION` |
| Object storage | Cloudflare R2 | 10 GB, and **no egress charge** — which for audio is the whole bill |
| A web service | Render, Railway, Fly.io, Koyeb | Needs ~256 MB |

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

## What is not in this image

**`ai-worker` and `ai-api`.** They bake ~2.8 GB of models and will not run in a free tier's
memory. The application degrades honestly without them rather than breaking:

- typed memos work completely;
- voice memos are accepted, stored, and stay `queued` — the UI says "waiting for a worker";
- Ask answers 503 with a sentence saying the service is unavailable.

To get transcription back, deploy `ai-worker` separately somewhere with more memory and
point it at the same `DATABASE_URL` and the same bucket, with a hosted provider instead of
the local model:

```
STT_PROVIDER=groq
GROQ_API_KEY=<key>
ENRICH_PROVIDER=none
```

That combination needs no baked models at all, so the worker image is small. `ENRICH_PROVIDER=none`
skips titles and summaries, which is the part that needs a local LLM; memos still get a
transcript, and a title falls back to the first line.

## Limits of the owner model

Stated plainly, because the schema cannot enforce what the design gives away:

- **A claim link is a bearer credential.** Whoever has it has those memos. There is no
  second factor and no revocation.
- **A cleared cookie is a lost account** unless the claim link was saved somewhere. There is
  no email to recover through, because there is no email.
- **This is not a security boundary for anything sensitive.** It is the right trade for
  memos on a hobby deployment and the wrong one for anything a person would be harmed by
  losing.
