# Memo App

Record a voice memo or type one. It gets transcribed, enriched with a title,
summary and tags, and becomes full-text searchable.

> **Status: skeleton.** This file is scaffolded by MEMO-01 and finished by
> MEMO-26. Sections marked _TODO_ are not yet true — don't follow them as
> instructions yet.

## Quickstart

Requires Docker Desktop **running**. No database setup, no API keys, no `.env`.

```bash
git clone https://github.com/ReviveRT/memo-app.git
cd memo-app
docker compose up
```

Then open **<http://localhost:5173>**.

> **Open it on `localhost`, not a LAN IP.** Browsers only expose the microphone
> in a secure context — HTTPS or `localhost` — and outside one they do not refuse
> it, they remove it: `navigator.mediaDevices` is not defined at all. So on
> `http://192.168.1.5:5173` the **Record** button reports that it needs a secure
> context, and nothing in the console explains why. Typing memos works anywhere.

_TODO (MEMO-26): first-build time and image size, given the baked whisper weights._

## Recording

Press **Record**, speak, press **Stop**. The memo appears in the list as soon as
the API has stored it, and the transcript fills in when the worker gets to it.
**Discard** throws a recording away before it is uploaded — once it is sent there
is no delete yet.

The elapsed timer next to the button is there so you know the length before you
send it. Nothing enforces a duration at the moment; the `MAX_AUDIO_SECONDS` cap
below is applied in the worker and arrives with MEMO-13.

What _is_ enforced is size: a recording over `MAX_AUDIO_BYTES` (12 MiB) is refused
with a 413 naming the limit, and nothing is stored. That is on the order of half an
hour of speech — 26 minutes at 64 kbps, which is the rate `MAX_AUDIO_SECONDS`
assumes, though no recording here has been measured to confirm it — so the byte cap
is deliberately well clear of the 10-minute duration cap and should never be what
refuses a memo somebody meant to keep. It names the size too when there is one to name;
a file large enough for PHP to have dropped the bytes before the app saw it can
only be told that it was too large. Size stands in for duration at this point in
the pipeline because a WebM stream from `MediaRecorder` carries no duration to
check — see the note in `api/config/memo.php`.

The first press asks for microphone permission. If you refuse it, the browser
remembers — re-allow it from the icon at the end of the address bar, there is
nothing this app can do to re-prompt.

Each browser records a different container and all three are accepted as they
are: Chrome and Edge produce WebM, Safari MP4, Firefox Ogg — including when
Firefox's own `MediaRecorder` says it is producing WebM ([Mozilla bug
1501308](https://bugzilla.mozilla.org/show_bug.cgi?id=1501308)). The API
identifies the file from its bytes rather than from anything the browser
claims, and MEMO-13 normalizes it before transcription.

That identification is also what decides whether an upload is accepted at all:
anything that is not one of the containers the app can transcribe is refused
with a 422 naming what the file turned out to be. Renaming a document to
`.webm` does not get it past this, and neither does the `Content-Type` the
request carries.

## Searching

The **Filter memos** box narrows the list as you type. Plain words are matched by
stemming, so `meeting` finds "meetings" and `walk` finds "walked". Three extras
are worth knowing because nothing on the page advertises them:

| Type | To get |
| --- | --- |
| `"call the dentist"` | that exact phrase, in that order |
| `dentist -thursday` | memos about the dentist, minus any mentioning Thursday |
| `dentist or plumber` | either word |

Part of a word works too, anywhere in it: `reorg` and `organis` both find
"reorganise". Fragments shorter than three characters still match, but they match
a great deal, so expect the list to fill up.

A memo that is still being transcribed has no text to match yet, so it stays in
the list whatever the filter says, rather than vanishing in the seconds after you
record it. The line under the box says when that is happening.

## Configuration

Everything has a working default. `.env` is optional — copy `.env.example` to
`.env` only to override something:

```bash
cp .env.example .env
```

`.env` is gitignored and must stay that way. `.env.example` is the committed
reference and contains no real credentials.

### Environment variables

| Variable | Default | Controls |
| --- | --- | --- |
| `POSTGRES_USER` | `memo` | Postgres role created on first boot |
| `POSTGRES_PASSWORD` | `memo` | Password for that role (local dev only) |
| `POSTGRES_DB` | `memo` | Database name |
| `DATABASE_URL` | `postgresql://memo:memo@db:5432/memo` | Connection string used by the API and worker. Overriding `POSTGRES_PASSWORD` alone is enough — this default is composed from the three above |
| `APP_ENV` | `local` | Laravel environment name. Informational; nothing branches on it |
| `APP_DEBUG` | `false` | Off by default, including locally — a debug response carries the stack trace and resolved config, and `/api/*` is proxied to the browser. Detail is on stderr regardless |
| `LOG_CHANNEL` | `stderr` | Where Laravel logs. `stderr` is what `docker compose logs api` shows |
| `POSTGRES_PORT` | `5432` | Host port for Postgres. Change it if something else on your machine owns 5432 |
| `API_PORT` | `8080` | Host port for the API |
| `WEB_PORT` | `5173` | Host port for the frontend |
| `STT_PROVIDER` | `local` | Primary transcription provider: `openai` \| `local` \| `fake`. Only `fake` is implemented so far — see below |
| `STT_FALLBACK` | `local` | Provider used when the primary errors or its key is absent |
| `STT_MODEL` | `base` | Model for the chosen provider — the main cost lever on the hosted path |
| `OPENAI_API_KEY` | _(empty)_ | Optional. Enables hosted transcription |
| `ANTHROPIC_API_KEY` | _(empty)_ | Optional. Enables Claude enrichment |
| `ENRICH_MODEL` | `claude-opus-5` | Claude model for title/summary/tags/category |
| `MAX_AUDIO_BYTES` | `12582912` | Upload byte cap for the API edge (12 MiB). Anything larger is a 413. Raising it above `upload_max_filesize` (16 MiB, in `api/conf.d/uploads.ini`) silently breaks uploads instead of widening them — `/api/health` reports both numbers and flags the mismatch |
| `MAX_AUDIO_SECONDS` | `600` | Duration cap enforced in the worker after normalization |
| `WORKER_POLL_SECONDS` | `1.0` | How long an `ai-worker` replica waits after finding the queue empty. Bounds how long a new memo sits in `queued`, not how fast the queue drains |
| `AUDIO_DIR` | `/data/audio` | Audio path inside the containers, on the shared `audio` volume. Changing it needs a rebuild with a matching `--build-arg AUDIO_DIR` — see the note in `.env.example` |

### Transcription today

Only `STT_PROVIDER=fake` is implemented so far (MEMO-08); `local` and `openai`
arrive with MEMO-14. The default of `local` is still safe to leave alone — the
worker starts normally on it and text memos are unaffected, because a typed memo
carries its own transcript and never reaches a provider.

A **voice** memo is a different matter now that MEMO-10 can create one. Recording
and upload work on the default configuration, and the memo is stored — but the
worker has no provider to transcribe it with, so it reaches `status=failed` with
a `last_error` saying so rather than gaining a transcript. To watch the whole
queue run end to end before MEMO-14 lands, start the stack with the fake
provider, which returns a fixed sentence instantly and never opens the file:

```bash
STT_PROVIDER=fake docker compose up
```

### Using the hosted providers

Both API keys are optional and neither is needed for the app to work end to end.

- **No `OPENAI_API_KEY`** — transcription runs locally on faster-whisper. Slower
  on first use, no network, no cost.
- **No `ANTHROPIC_API_KEY`** — memos still transcribe, store and search. They get
  a fallback title (first 60 characters of the transcript) and `enrichment_error`
  is recorded on the row.

To use either, paste your own key into `.env`. _TODO (MEMO-26): what measurably
changes when you do._

## Repository layout

```
db/migrations/   numbered SQL migrations, applied in filename order
api/             PHP API (Laravel on FrankenPHP)
ai/              Python worker — transcription and enrichment
web/             frontend
```

Laravel's own migrations, Eloquent, queue, cache and session tables are all
unused: the schema is owned by `db/migrations/` and applied by `db/migrate.sh`,
persistence goes through PDO with prepared statements and no ORM, and the job
queue is the `memos` row itself, consumed by the Python worker. `APP_KEY` is
intentionally unset — nothing here encrypts.

## Architecture

_TODO (MEMO-26): short version here; the reasoning and trade-offs live in
NOTES.md (MEMO-27)._

## Assumptions

- Single user. No authentication, no multi-tenancy.
- Local-only. Not hardened for a public deployment.
- Audio is kept on a Docker volume, not object storage.

## Development

### Running the worker's tests

`pytest` is not in the `ai` image — it installs `requirements.txt` only, the same
way the api image runs `composer install --no-dev` — so the suite installs the dev
dependencies first and runs in one throwaway container:

```bash
docker compose run --rm --no-deps --user 0:0 --entrypoint sh ai-worker -c 'pip install -q -r requirements-dev.txt && python -m pytest'
```

One invocation, because each `docker compose run` starts from the image again and
the install does not persist. `--user 0:0` because the worker runs as a non-root
user that cannot write into site-packages. No database is needed: the tests that
would need one are the claim and the fence, and those are verified against a real
Postgres instead — `ai/memo_ai/memos.py` records what those runs showed.

_TODO (MEMO-26): running the api tests, running a service outside Docker, applying
a new migration._
