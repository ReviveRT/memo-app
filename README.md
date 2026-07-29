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

> Open it on `localhost`, **not** a LAN IP. The microphone silently fails outside
> a secure context, with no error in the console.

_TODO (MEMO-26): first-build time and image size, given the baked whisper weights._

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
| `DATABASE_URL` | `postgresql://memo:memo@db:5432/memo` | Connection string used by the API and worker |
| `STT_PROVIDER` | `local` | Primary transcription provider: `openai` \| `local` \| `fake` |
| `STT_FALLBACK` | `local` | Provider used when the primary errors or its key is absent |
| `STT_MODEL` | `base` | Model for the chosen provider — the main cost lever on the hosted path |
| `OPENAI_API_KEY` | _(empty)_ | Optional. Enables hosted transcription |
| `ANTHROPIC_API_KEY` | _(empty)_ | Optional. Enables Claude enrichment |
| `ENRICH_MODEL` | `claude-opus-5` | Claude model for title/summary/tags/category |
| `MAX_AUDIO_BYTES` | `12582912` | Upload byte cap enforced at the API edge (12 MiB) |
| `MAX_AUDIO_SECONDS` | `600` | Duration cap enforced in the worker after normalization |
| `AUDIO_DIR` | `/data/audio` | Audio path inside the containers, on the shared `audio` volume |

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
api/             PHP API (Slim 4 on FrankenPHP)
ai/              Python worker — transcription and enrichment
web/             frontend
```

## Architecture

_TODO (MEMO-26): short version here; the reasoning and trade-offs live in
NOTES.md (MEMO-27)._

## Assumptions

- Single user. No authentication, no multi-tenancy.
- Local-only. Not hardened for a public deployment.
- Audio is kept on a Docker volume, not object storage.

## Development

_TODO (MEMO-26): running tests, running a service outside Docker, applying a new
migration._
