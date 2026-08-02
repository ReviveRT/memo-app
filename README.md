# Memo App

Record a voice memo or type one. It gets transcribed, given a short title, and
becomes full-text searchable. Keep the quick ones loose, gather the rest into
named collections, and set a reminder on anything that needs to come back to you.

Two screens: a landing page at `/`, and the app itself at `/memos`.

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

### What the first build costs

**Three to four minutes, and 6.7 GB of disk for the `ai-worker` image alone.**
Nothing after the first build pays either cost again.

That is the price of "no account, no key, nothing to sign up for", and it is worth
naming rather than letting you discover it. `docker compose build` downloads 2.8 GB
of model weights and bakes them into the image, so the running app never fetches a
model and works with networking switched off. See
[Transcription](#transcription) for which models and why.

The weights are fetched at pinned commits, so a build today and a build next year
produce the same transcripts — the accuracy table below is a measurement of
specific bytes, and one of these repositories has already changed hands once.

Measured on an M-series Mac with a fast connection, rebuilding every layer except
the `python:3.12-slim` base, which was already pulled:

| | |
| --- | --- |
| Total build, `ai-worker` | 3 min 18 s |
| — of which, fetching 2.8 GB of weights | 52 s |
| `docker images` size, before / after | 1.27 GB → 6.74 GB |

Two honest caveats about those numbers. The 52 s is bandwidth, so on a 50 Mbit/s
link expect closer to eight minutes for that step alone. And the 6.74 GB is what
`docker images` reports, which on Docker's containerd store adds two things: the
unpacked image (3.8 GB) and the compressed layers kept beside it (3.0 GB). Both are
real disk, and neither is the 2.8 GB of weights on its own — `docker system df`
breaks the total down if you need to reclaim space.

Rebuilding after a code change does **not** refetch the weights — the model layer
sits above `COPY`, so an edit to the worker rebuilds in about three seconds.

Upgrading from an earlier revision leaves one orphan behind, since the cache volume
was renamed when it stopped holding whisper's weights:

```bash
docker volume rm memo-app_whisper-cache
```

**Build for your own architecture.** On Apple silicon that is the default and needs
no flag. Do not pass `--platform linux/amd64`: everything here runs on CPU — there
is no Metal backend for CTranslate2 and no GPU passthrough into a Linux container
on macOS — so an emulated x86 image is not slower, it is unusable. `NOTES.md` has
the detail.

## Recording

Press **Record**, speak, press **Stop**. The memo appears in the list as soon as
the API has stored it, and the transcript fills in when the worker gets to it.
**Discard** throws a recording away before it is uploaded — once it is sent there
is no delete yet.

The elapsed timer next to the button is there so you know the length before you
send it. A recording longer than `MAX_AUDIO_SECONDS` (10 minutes) is **accepted,
stored, and then failed by the worker** rather than refused at the door — the memo
appears in the list and turns into a failure naming both its length and the limit.
That is not a compromise, it is the only place the check can happen: the duration
is not known until the worker has re-encoded the file. The next section explains
why.

What _is_ enforced at the door is size: a recording over `MAX_AUDIO_BYTES` (12
MiB) is refused with a 413 naming the limit, and nothing is stored. The recorder
asks for 48 kbps, so that is about **34 minutes** — comfortably past the 10-minute
duration cap, which is the limit meant to stop a long memo, and which says so in
words about length rather than megabytes.

Left to the browser's own default it would not be. Measured through the app in
Chromium: the default is 128 kbps, 153 kbps once the WebM container is counted, which
puts a ten-minute memo at 11.5 MB against a 12.6 MB cap — 9 percent of margin. Opus is
variable-bitrate, so whether such a memo was refused for length or for size would come
down to how much of it was silence. Asking for 48 kbps separates the two caps, and it
costs nothing downstream: transcription resamples to 16 kHz regardless, so everything
above that is discarded either way. It names the size too when there is one to name;
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
claims, and the worker normalizes it before transcription.

That identification is also what decides whether an upload is accepted at all:
anything that is not one of the containers the app can transcribe is refused
with a 422 naming what the file turned out to be. Renaming a document to
`.webm` does not get it past this, and neither does the `Content-Type` the
request carries.

### Normalization, and where the duration comes from

Before transcription the worker runs every recording through ffmpeg to 16 kHz
mono, then measures the result with ffprobe. Both binaries live in the `ai`
image; the PHP image has neither.

This is not a cost saving, and it is worth saying so because it looks like one.
Hosted transcription is billed per minute of audio _duration_, so resampling
changes the bill by exactly zero. It is there for two other reasons:

- **One decode path.** Chrome WebM, Firefox Ogg and Safari MP4 stop being three
  problems after this step.
- **A duration you can trust.** This is the load-bearing one. Chrome streams its
  WebM to a sink it cannot seek back into, so it never returns to fill in the
  duration — the file arrives with no Duration element and `ffprobe` answers
  `N/A`, not a number. Measured across the three browsers, Chrome is the only one
  that does this: Firefox's Ogg and Safari's MP4 both carry a duration. That does
  not help, because nothing upstream knows which browser sent a given file — and
  Safari's number is 39 ms longer than its own audio, from AAC encoder delay. So
  the source duration is either missing or slightly wrong, and the one measured
  after normalization is neither. That is why `MAX_AUDIO_SECONDS` is enforced in
  the worker and cannot be enforced at the API edge, and why a memo over the cap
  fails after being stored rather than being refused with a 413 like an oversized
  one.

The output is Opus at 24 kbps, not WAV, and that matters more than it sounds.
16 kHz mono WAV of a ten-minute memo — the longest this app accepts — is 19.2 MB,
which is 77 percent of OpenAI's 25 MB request limit. The app uploaded that same
memo in about 3.7 MB, so normalizing to WAV would mean carrying roughly five times
what the user actually recorded, and most of the request budget, for a format
nothing downstream asked for. Opus is a fraction of either. WAV is kept as an
option for a provider that decodes in-process, and the `local` one asks for it —
it reads the file itself, so a codec in between is two conversions for bytes that
never leave the container.

One consequence worth knowing before you go looking for a bug: an Opus stream
always reports `48000` as its sample rate, whatever it was encoded from — the
format fixes the decoder's rate. The 16 kHz is real (everything above 8 kHz is
gone, and the file is a fraction of the size), but `ffprobe` will not say `16000`
unless you ask for the WAV output.

### What happens to a memo, and what happens when it goes wrong

A memo is its own queue row: `POST /api/memos` writes it `queued`, a worker replica
claims it, and the claim is what moves it to `processing`. There is no jobs table
and nothing to reconcile after a crash.

**The job commits twice.** When transcription succeeds, the transcript is written
and the memo stays `processing`. Only the second commit publishes it as `ready`,
with a title, and that second commit runs whether or not enrichment worked. The
split buys two things:

- **A transcript is never lost, and never paid for twice.** A worker killed between
  the two commits leaves the text on the row, and the re-claim sees it and skips
  straight to enrichment. On a hosted provider that is the difference between one
  bill and two for the same recording.
- **Enrichment cannot fail a memo.** A title and a summary are conveniences; the
  words are the memo. So a failed enrichment lands in `enrichment_error` on a
  `ready` row, and `failed` means one thing only: no transcript.

**A memo is never untitled,** and it is titled by whichever of four sources can do
best. An enricher's title if one ran (MEMO-21); otherwise whatever is already on the
row, which is what you typed if you renamed it; otherwise a short phrase
`ai/memo_ai/titles.py` cuts out of the transcript — "Meeting with my friend John"
from "Tomorrow I will have a meeting with my friend John at 15am"; and failing all
of those, the first line of the transcript cut to 60 characters, which is the same
rule the frontend uses to label an untitled memo.

The last of those is computed in SQL, and that is why it is still there: the reaper
publishes rows in bulk with no job in memory to cut a phrase out of anything.

**Failures are retried only when retrying could help.** A recording ffmpeg cannot
decode, or one over `MAX_AUDIO_SECONDS`, fails on the first attempt: the same file
will not decode on the third, and two more attempts would spend 90 seconds to say
the same thing. What is retried is a provider that cannot run *right now* — a model
still downloading, a load that ran out of memory — and an error nobody classified,
on the grounds that nobody has shown it to be deterministic either. Three attempts,
30 seconds then 60, jittered.

**A worker that dies takes nothing with it.** The attempt count is incremented by
the claim rather than by the failure write, so it survives a `SIGKILL` — which is
the only reason a memo that crashes its worker terminates at all. A claim held
longer than `REAP_AFTER_SECONDS` is taken back by whichever replica notices, and
the memo is requeued, or resolved if its attempts are gone: `failed` if it never
produced a transcript, `ready` if it did. Because there are two replicas, every
result write is conditional on still holding the claim it started with, so a reaped
job and the original still running cannot both write.

**A failed memo says why, and you can put it back.** Whatever refused the recording
wrote a sentence for the person who made it — "No speech was detected in this
recording. It may be silent, too quiet for the microphone that captured it, or cut
short before anything was said." — and that is what the card shows in place of a
transcript, in the detail view, and in the corner toast. None of them ever carry a
stack trace or a provider's raw error body: ffmpeg's stderr and an unclassified
exception both go to the log and the row gets a sentence this project wrote.

Under the reason is a **Retry** button, and it is there because most of those
sentences describe something you can fix. The worker's own three attempts are over
within a couple of minutes of the recording, so by the time you have read the reason,
set the key or changed the model, nothing left in the stack will touch that memo
again. Retry resets its attempts, makes it due immediately, and puts it back in the
queue — the card goes from `failed` to `queued` and the page starts polling again on
its own. Only a `failed` memo can be retried; pressing it on one a worker is already
holding would put two replicas on the same recording, so the API refuses with a 409
that names the state it actually found.

**An empty recording gets no card at all.** Record four seconds of silence, or with
the microphone muted, and there is nothing to keep: no words were said, and no retry
will find any. So that memo is deleted — the row and the recording — and the only
thing left is the corner toast saying why. Every *other* failure keeps its card,
because the recording is real and the fault is usually not yours.

The two are told apart by `last_error_code`, a short token the worker writes beside
the sentence — `no_speech` and `no_audio` are discarded, everything else is kept. The
sentence is prose meant to be read and reworded; matching on its text to decide
whether to delete somebody's recording is the mistake that column exists to prevent.
A code neither side recognises keeps the memo, which is the safe direction to be
wrong in.

You can watch all of it in `docker compose logs -f ai-worker`.

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

### Filtering by date

Next to every search box is the same set of date filters: **Any time**, **Today**,
**Yesterday**, **Last 7 days**, and **Custom…** for a range like 19–23 July.

Two things about it are worth knowing, because both are invisible when they work:

- **Days are your days.** "Yesterday" is worked out in your own timezone by the
  browser, which converts it to a pair of absolute times before asking the API.
  The API has no timezone setting to get wrong, and there is no `tz` parameter.
- **The end date is included.** Picking 23 July includes everything written on
  the 23rd, up to midnight. Internally the range is half-open — `from` inclusive,
  `to` exclusive — and the browser adds the extra day; see `useDateRange.js` and
  `App\Support\TimeWindow` for why that beats ending at 23:59:59.

The date filter is a hard bound, unlike the text filter: a memo still being
transcribed is pinned into a filtered list, but it is **not** pinned past the
dates or into a collection it is not in. A memo recorded today does not appear
under "Yesterday".

## Collections

A collection is a named folder — "Memos for Work", "Errands". Create one under
**Collections**, then open any memo and pick it from the **Collection** menu. A
memo lives in one collection or in none; the ones in none are the **fast memos**
in the strip at the top, which is what that list means.

Deleting a collection **does not delete its memos**. They go back to being fast
memos. That is the `ON DELETE SET NULL` on `memos.collection_id`, so it holds
however the row is deleted.

Searching collections reaches further than their names: it also matches the memos
filed inside them. Searching `dentist` finds the collection holding the dentist
memo, even if the collection is called "Errands".

## Reminders

Open a memo and set either an **alarm** (a date and time) or a **timer** (5
minutes to tomorrow), with an optional note. Both are the same thing underneath —
one absolute instant — so the two controls are two ways of choosing when.

> **Reminders fire while the app is open in a tab.** There is no service worker
> and no Web Push, so nothing reaches you with the app closed. That is a real
> limit rather than a bug, and the card says so on screen.

What happens when one comes due:

| When it comes due | What you get |
| --- | --- |
| App open | A system notification, plus a card in the corner |
| App closed | The cards only, all together, next time you open it |

The second row is deliberate: coming back on Monday to eleven overdue reminders
should not fire eleven system notifications at once.

Permission for system notifications is asked for the first time you set a
reminder — never on page load, because an unprompted request is suppressed by
browsers and a denial is permanent. Refusing it does not break anything; the
in-app cards still appear.

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
| `STT_PROVIDER` | `local` | Transcription provider: `local` \| `fake` \| `openai`. `openai` is recognised but not built — see below |
| `STT_FALLBACK` | `local` | Provider used when the primary cannot run at all. Not used when a recording simply produced no words |
| `STT_MODEL` | `large-v3-turbo` | Whisper size for the `local` provider. The accuracy lever — see the table below before changing it |
| `STT_LANGUAGE` | _(empty)_ | ISO code of your recordings (`en`, `ru`, …). Empty detects it per recording. Setting it is ~30% faster and safer on short or accented audio |
| `OPENAI_API_KEY` | _(empty)_ | Read by nothing today. Passed through for whoever writes the hosted adapter |
| `ANTHROPIC_API_KEY` | _(empty)_ | Optional. Enables Claude enrichment |
| `ENRICH_MODEL` | `claude-opus-5` | Claude model for title/summary/tags/category |
| `MAX_AUDIO_BYTES` | `12582912` | Upload byte cap for the API edge (12 MiB). Anything larger is a 413. Raising it above `upload_max_filesize` (16 MiB, in `api/conf.d/uploads.ini`) silently breaks uploads instead of widening them — `/api/health` reports both numbers and flags the mismatch |
| `MAX_AUDIO_SECONDS` | `600` | Duration cap, enforced in the worker after normalization because that is the first point a duration exists. A memo over it is stored and then failed, not refused. Zero or negative is refused at boot |
| `WORKER_POLL_SECONDS` | `1.0` | How long an `ai-worker` replica waits after finding the queue empty. Bounds how long a new memo sits in `queued`, not how fast the queue drains |
| `MAX_ATTEMPTS` | `3` | How many times a memo may be claimed, counting the first. Only failures that might resolve on their own are retried at all — see below |
| `RETRY_BACKOFF_SECONDS` | `30` | Base of the exponential backoff between attempts, doubling and jittered ±20% |
| `REAP_AFTER_SECONDS` | `3600` | How long a memo may sit in `processing` before a worker assumes the one that claimed it is gone. **Must exceed the longest a healthy job can take** (2,880s at the defaults). Raising `MAX_AUDIO_SECONDS` raises that ceiling; the worker recomputes it at boot and warns if the lease no longer clears it |
| `REAPER_INTERVAL_SECONDS` | `60` | How often each replica looks for expired leases |
| `AUDIO_DIR` | `/data/audio` | Audio path inside the containers, on the shared `audio` volume. Changing it needs a rebuild with a matching `--build-arg AUDIO_DIR` — see the note in `.env.example` |

### Transcription

Recordings are transcribed **on your machine**, by
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) running inside the
`ai-worker` container. There is no account to create, no key to paste and no
per-minute bill; the only thing it spends is CPU. The weights are MIT-licensed and
so is everything that runs them.

**It never downloads a model while you are using it.** The weights are fetched
once, during `docker compose build`, and baked into the `ai-worker` image — so the
running stack has no reason to reach the network at all and works with networking
switched off. That is the point of paying for it at build time: the alternative is
a first recording that hangs for minutes on a slow connection, or fails outright
offline, which would quietly make "no account, no key" untrue.

Three models are baked, totalling **2.8 GB**: `large-v3-turbo` for transcription
(1622 MB), `tiny` for the per-recording language detection (78 MB), and
Qwen2.5-1.5B-Instruct Q4_K_M for enrichment (1117 MB). All three are public
repositories needing no account and no token — the whisper weights are MIT and
Qwen is Apache 2.0. See [Quickstart](#quickstart) for what that costs you in build
time and disk.

The `model-cache` volume is still mounted, and now holds nothing on a default
install. It is where a model you asked for but nobody baked gets cached — see
[Choosing a model](#choosing-a-model).

The language is detected per recording rather than configured, so one stack takes
memos in several languages; the Russian test recording is identified as Russian
with nothing told to it. Set `STT_LANGUAGE=en` if you only ever speak one — it is
about 30% faster and removes a misdetection risk that is real on short clips.

### Choosing a model

`STT_MODEL` is the accuracy lever and it matters more than it looks. Measured on a
real recording of _"I would like to place an order"_, spoken in an Indian accent:

| `STT_MODEL` | Disk | Transcribed it as |
| --- | --- | --- |
| `base` | 142 MB | "I would like to **blaze a door there**" |
| `small` | 464 MB | "I would like to **blaze an order**" |
| `medium` | 1.5 GB | correct |
| **`large-v3-turbo`** _(default)_ | 1.6 GB | correct |

turbo is not the slow choice despite being the largest. It pairs a `large-v3`
encoder with a four-layer decoder, so it beats `small` on speed and is three times
faster than the `medium` it out-transcribes.

**Only the default is baked into the image.** Setting `STT_MODEL` to anything else
puts you back on the old behaviour: the worker resolves the name against
HuggingFace on first use and caches it in the `model-cache` volume, so that run
needs the internet and the first memo may fail with _"the local transcription
model is still being downloaded"_ while it finishes. Record another a minute later.

To bake a different one instead — worth doing if you are short of disk and your
speakers are easy to understand — change `DEFAULT_STT_MODEL` in
`ai/memo_ai/config.py` and build with matching arguments:

```bash
docker compose build --build-arg STT_MODEL=base --build-arg STT_MODEL_REVISION= ai-worker
```

Both parts matter. The config decides what the worker asks for and the build
argument decides what the image carries, so changing only the argument gives you an
image that carries `base` and still downloads turbo the first time somebody
records. The empty `STT_MODEL_REVISION` clears the pinned commit, which belongs to
turbo's repository and does not exist in `base`'s — leave it set and the build
fails on a 404 rather than silently fetching something else.

### Speed

A memo of a few seconds transcribes in a few seconds. Anything over two minutes of
audio switches to **batched** decoding — the recording is cut at silence and the
encoder runs several of those windows at once instead of walking them in series —
which is worth four to five times on long audio. Measured on an idle machine with
the shipped model:

| Recording | In series | Batched | What runs |
| --- | --- | --- | --- |
| 3 seconds | 8.0 s | 8.4 s | series |
| 13 seconds | 8.7 s | 9.1 s | series |
| 2 minutes | 143 s (0.97× realtime) | **27.5 s (0.23× realtime)** | batched |
| 10 minutes _(the cap)_ | ~10 min | **~2 min** | batched |

Short memos get their speed somewhere else, and it is the larger factor of the
two. Working out what language a recording is in costs a **whole extra encoder
pass** over the first 30-second window — on a model whose encoder is the entire
bill for a short memo, that simply doubles the job. So the question is put to
`tiny` instead, which answers in 0.21 s against turbo's 4.44 s and, on every real
recording tested, reached the same verdict. Measured end to end on real
recordings, one at a time:

| Recording | Detecting on the big model | **Detecting on `tiny`** |
| --- | --- | --- |
| 3 seconds | 9.5 s | **5.1 s** |
| 13 seconds | 9.0 s | **5.2 s** |

Nothing is given up for it: the language is still detected per recording, so a
stack nobody configured still transcribes Russian as Russian. If `tiny` is unsure
the guess is thrown away and the big model works it out itself, paying the old
cost on that memo alone.

Batching is not used below that threshold because it does not help and it does
hurt. Short memos are a single window, so there is nothing to run in parallel —
and because each window is decoded independently, whisper loses the running
context that keeps its formatting consistent. The same clips come back
measurably worse, reproducibly:

```
"...need this to be write down at 12 p.m."  →  "...to be right down at..."
"1, 2, 3, 4, 5, 6, 7, 8, 9, 10"             →  "one two three four five..."
```

Numerals and punctuation are worth keeping in a column that gets full-text
searched. Past two minutes the arithmetic inverts — nobody trades eight minutes of
waiting for a comma — so the threshold is where it is.

**`STT_LANGUAGE=en`** is still worth setting if you only ever dictate in one
language, but it is now a small win rather than a large one — it skips `tiny`'s
0.2 s and, more usefully, removes any chance of a wrong guess. Language detection
is unreliable on short or accented audio whatever model does it.

Raising CTranslate2's thread count is *not* worth it: it was tried, and bought 10%
on a long memo while making short memos slower and costing 890 MB per replica.

The floor on a short memo is one pass of the large-v3 encoder, and whisper pads
every input to 30 seconds before running it — so three seconds of audio costs what
thirty would. That is the architecture, not a setting. The only lever left below
~5 s is a smaller model, and the table above says what that costs.

### What it costs to run

Memory, mostly, and the two decisions compound: turbo is 1.1 GB resident, and
batching takes the peak to **2.4 GB per replica**. With `replicas: 2` that is
about 4.8 GB before anything else in the stack, which is most of a default Docker
Desktop VM. If that is too tight, `STT_MODEL=base` or one worker replica are the
two levers, in that order.

Disk is the other half, and it is paid once rather than per replica — the baked
weights live in the image, which both replicas share. See
[Quickstart](#quickstart) for the number.

### Repeated words

Say the same word ten times and whisper will happily write it two hundred times.
It is the best-known failure mode of the model family: the decoder gets stuck
re-emitting a token and runs to its own ceiling, which also makes the memo slow,
because every one of those words has to be generated. A real 4.3-second recording
of "Rock" said ten times came back as **223** of them, taking 21 seconds.

Two settings fix it together and neither works alone — `temperature=0` and a
`repetition_penalty` of 1.1. The penalty by itself is erratic rather than helpful,
because when whisper judges its own output degenerate it retries at a higher
temperature, and that means sampling. Six runs of the same audio:

| | "Rock" count, six runs |
| --- | --- |
| default | 223, 223, 223, 223, 223, 223 |
| penalty only | 0, 0, 1, 6, 9, 223 |
| **both** | **11, 11, 11, 11, 11, 11** |

The same recording is now 11 words in 10 seconds rather than 223 in 21. Dropping
the temperature ladder also makes every transcript **deterministic** — the same
audio gives the same text every time, which it did not before.

Things that did **not** help, tried on the same recording so you need not: a
domain `initial_prompt`, `beam_size=10`, greedy decoding, loudness-normalizing the
audio, pinning the language, and switching between the WAV and Opus intermediate
formats. Model capacity was the lever.

There _is_ an `initial_prompt` set now, for the unrelated problem in the next
section, and it does not bring the runaway back — but it does move these counts, in
both directions. Baseline against primed, twice each on the four degenerate
recordings here:

| Recording | Baseline | Primed |
| --- | --- | --- |
| "Rock" ×11, 4.3 s _(two uploads)_ | 11 | 12 |
| "Rock" ×11, 6.2 s | 11 | **5** |
| "Rocka" ×15, 6.8 s | 15 | 17 |

All deterministic, none a runaway; the 5 is the primer reading a wall of identical
words as prose and stopping early, which is the safer direction of the two. None of
this is a shape human speech takes — these clips exist to provoke the decoder — but
one candidate wording of that prompt _did_ reintroduce the runaway on a different
recording, so treat the prompt as part of this failure mode's surface rather than a
free slot.

One thing that did, and it is not a knob you have to turn. The voice-activity
filter that keeps whisper from inventing words over silence ships with 400 ms of
padding around each speech region, and that was enough to swallow the opening
consonant of "place" — turbo returned "blaze an order" with it and "place an
order" without it. The padding is 1000 ms here, checked against the
filter-disabled baseline on five real recordings, which keeps the transcript
honest without giving up silence detection. Silence still comes back as _"No
speech was detected"_ rather than as whisper's habitual "Thank you."

### Punctuation and paragraphs

Whisper was trained on both punctuated and unpunctuated transcripts, and which
style you get is a property of the audio rather than a setting you can pick. When
it goes the wrong way there is nothing subtle about it. A real 89-second memo
recorded into this app came back as 1204 characters of lowercase words with **not
one comma in them** — a single unbroken run reading `database is running from yet
another …` and never stopping — while every short memo from the same session was
punctuated correctly.

The fix is an `initial_prompt`. Whisper receives it as though it were the
transcript of the audio immediately preceding this one, so a primer that is
punctuated, capitalized and made of ordinary sentences is a demonstration of the
register to continue in — not an instruction. On that recording, measured once per
setting, with the first wording of the primer rather than the one shipped:

| | Result |
| --- | --- |
| default | lowercase blob, 13 segments |
| `condition_on_previous_text=False` | lowercase blob, 13 segments |
| **`initial_prompt`** | **punctuated, 7 segments** |
| `initial_prompt`, no conditioning | punctuated for four sentences, then blob |

The last row is the one that explains the mechanism, and it is why
`condition_on_previous_text` is deliberately left at its default of `True`. The
primer only reaches the first 30-second window directly; what carries its register
through the rest of a long recording is whisper conditioning each window on the
text of the one before. It costs 28.2 s against 27.7 s on that recording. The
wording actually shipped punctuates the same 89 seconds just as fully — 10 segments,
fifteen sentences, three paragraphs.

The **wording is load-bearing**, and both halves of it were paid for. A first
version was prose with no numeral in it and it taught the model to spell numbers
out — the browser fixture that transcribes as `1, 2, 3, …` came back as "One, two,
three, …", which is a real loss in a column that gets full-text searched. A third
variant, also with digits but phrased differently, reintroduced the repetition loop
from the previous section: the same 89 seconds came back with _"Let me clean up."_
25 times over. Editing `PUNCTUATION_PRIMER` means re-running the fixtures in
`ai/tests/fixtures/` plus one long recording.

The English primer was checked against the non-English fixture rather than assumed
safe: the Russian recording still comes back Russian, detected at 0.99, with its
words unchanged, because the language token decides the language and the prompt
only suggests a register. What is _not_ claimed is that it punctuates a long
non-English recording as well as it does this one — there is no long non-English
recording here to find out on.

#### Shaping, after the model

`ai/memo_ai/prose.py` then does the typographic half, which a punctuated transcript
still needs: spacing around punctuation, a capital starting each sentence, the
English pronoun `I`, a full stop on a transcript that ends without one, and
paragraphs so a long memo is not one block. Its one invariant is that **no word is
ever added, removed, reordered or respelled** — every rule touches whitespace,
punctuation or letter case and nothing else, and a test asserts it over every
fixture in the suite. A formatter that quietly improved somebody's wording would be
indistinguishable from a transcription error.

What it deliberately does not do is restore punctuation from the words. That is a
real problem whose real solutions are all models, and guessing clause commas from
English word order with regular expressions produces confident nonsense. The primer
above is what makes the model punctuate; this layer only tidies what it produced.

The paragraphs are **counted rather than heard**, and that is a concession. Reading
breaks off the pauses in the recording is much better evidence — a speaker who
stops for two seconds has changed subject — but the evidence is not there to read:
whisper's segments tile the audio, so `segment.end` is the next segment's `start`
and the gap between them is always exactly `0.00`. Only two of the seven real
recordings here have more than one segment — the rest decode as a single span and
offer no gap to measure at all — and across those two, one on each decode path, run
with and without the primer, that is 28 inter-segment gaps and every one is zero. The
segment boundaries are 30-second window cuts and land mid-sentence, so they carry
no structure either. Genuine pause data does exist one level down, in
`word_timestamps=True`, and that is where to start if you want the idea back; it
was declined for now because it costs an extra alignment pass on every memo, and
because the primer above is already proof that perturbing the decode options can
break something else.

Two other providers exist behind the same interface:

| `STT_PROVIDER` | What happens |
| --- | --- |
| `local` _(default)_ | faster-whisper, as above |
| `fake` | A fixed canned sentence, instantly. Useful for watching the queue work without waiting on a model |
| `openai` | Recognised, deliberately **not built**. See below |

```bash
STT_PROVIDER=fake docker compose up
```

`STT_FALLBACK` names the provider to use when the primary cannot run *at all* —
not built, model missing, out of memory. It does **not** retry a recording that
simply produced no words, because both providers are handed the same normalized
audio and the second would reach the same answer more slowly. With the shipped
defaults the two are equal and there is no chain at all.

### Why there is no hosted provider

`openai` is a name the configuration accepts and nothing implements, and that is a
decision rather than an unfinished edge. Writing an adapter against an API this
project has never called would mean shipping a code path nobody has run, in the
one place where "it looks right" and "it works" are hardest to tell apart. What
proves the seam instead is that two providers really do go through it — `local`
and `fake` — with different formats, different failure modes and different costs.

Setting it is not a dead end: `openai` reports itself unavailable, the worker logs
that it was skipped, and `STT_FALLBACK` transcribes the memo. The row records the
provider that actually ran, so `memos.stt_provider` never claims a request was
made that was not.

Pricing it needs no invoice, either. Hosted transcription bills per minute of
audio, so the levers are the model and `MAX_AUDIO_SECONDS` — not the sample rate
— and MEMO-22 keeps the rate table that turns "10,000 memos" into a number.

### Using the Anthropic key

`ANTHROPIC_API_KEY` is optional and nothing here needs it. Without it, memos still
transcribe, store and search, and every one of them still gets a real title:
`ai/memo_ai/titles.py` cuts one out of the transcript with no model, no key and no
network — it strips the throat-clearing and the date a spoken memo opens with, cuts
at the first clause, and caps what is left at six words. "Tomorrow I will have a
meeting with my friend John at 15am" becomes "Meeting with my friend John".

No enricher runs at all today, so nothing is attempted and `enrichment_error` stays
NULL — that column carries a sentence only when an enricher exists and fails, which
is MEMO-21's to produce.

What the key would buy is the things a heuristic cannot do: a summary, tags, and a
title that reads a list of eighteen document names and answers "Documents for the
job". Nothing writes `summary`, `tags` or `category` today; those columns exist and
stay NULL. NOTES.md has the argument for why the local titler is what ships, and
where a model would plug in.

Every title is editable, from the memo's own card, precisely because a guess this
cheap is sometimes going to be wrong.

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

## HTTP API

Everything is under `/api`, served by the `api` container and proxied by the dev
server so the browser sees one origin. No authentication — see Assumptions.

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/health` | Database round trip and upload limits; 503 when it fails |
| `GET` | `/memos` | The list, newest first. See the parameters below |
| `POST` | `/memos` | Create one: JSON `{text}`, or `multipart/form-data` with `audio` |
| `PATCH` | `/memos/{memo}` | `{collection_id}` — file it, or `null` to unfile. `{title}` — rename it, or `null` to clear. Either field, or both |
| `POST` | `/memos/{memo}/retry` | Send a failed memo back to the queue. No body. 409 if it is not `failed` |
| `GET` | `/memos/{memo}/audio` | The original recording, with byte ranges. 404 for a typed memo |
| `DELETE` | `/memos/{memo}` | 200 with the memo it removed. Takes the recording and any reminders with it |
| `GET` | `/collections` | The grid, with each collection's memo count and newest labels |
| `POST` | `/collections` | `{name}` |
| `PATCH` | `/collections/{collection}` | `{name}` — rename |
| `DELETE` | `/collections/{collection}` | 204. Its memos survive as fast memos |
| `GET` | `/reminders` | Every reminder still owed, for the browser's delivery loop |
| `POST` | `/memos/{memo}/reminders` | `{remind_at, note?}` |
| `PATCH` | `/reminders/{reminder}` | Mark it shown. No body; idempotent |
| `DELETE` | `/reminders/{reminder}` | Remove it |

`GET /memos` takes four independent filters, and any combination is valid:

| Parameter | Meaning |
| --- | --- |
| `q` | Full-text plus substring match over title, summary, transcript and tags |
| `from`, `to` | ISO 8601 instants. `from` inclusive, `to` **exclusive** |
| `collection` | A collection id, or `none` for memos in no collection |
| `limit` | Default 50, max 200. Rejected rather than clamped |

`GET /collections` takes `q`, `from`, `to` and `limit` (max 100), spelled the same
way and meaning the same things.

### Playing a recording back

`GET /api/memos/{id}/audio` answers with the file as it was uploaded, under the
media type it was sniffed as, and it honours `Range`:

```bash
curl -s -D- -o /dev/null -H 'Range: bytes=0-1023' http://localhost:8080/api/memos/<id>/audio
```

```
HTTP/1.1 206 Partial Content
Accept-Ranges: bytes
Content-Length: 1024
Content-Range: bytes 0-1023/24775
Content-Type: video/webm
```

That is not a refinement — Safari refuses to play audio from an endpoint that
answers a ranged request with the whole file, and no browser can seek without it.
A range past the end of the file is a `416` carrying the real size; `HEAD` answers
the length and `Accept-Ranges` with no body, which is what a player asks first.

Two 404s, with different sentences on purpose: a typed memo has no recording and
never did, while a row naming a blob the volume does not have is a stack that has
lost data — `docker compose down -v` does exactly that, since the database and the
recordings are on separate volumes.

The recordings are cached hard (`private, max-age=31536000, immutable`). Nothing
rewrites `audio_path`, so the bytes under a memo id cannot change; the URL either
answers with the same file or stops existing with the memo.

**Chrome and Edge recordings declare no duration, and the player works around it.**
A WebM from `MediaRecorder` carries no Duration element — the same fact that makes
the worker measure length with `ffprobe` after normalization instead of at the
upload edge — so `audio.duration` is `Infinity` and a native scrubber has nothing
to size itself from. Opening a memo seeks once past the end of the file, which is
what makes the element discover the real length, and then resets to the start. It
costs one range request, which is the other half of why this endpoint supports
them.

Two response conventions worth knowing before writing a client:

- **Every write about a reminder answers with the memo**, not the reminder —
  `{"memo": {...}}` — because the memo already carries its reminders and the
  frontend reconciles its lists by memo id. `GET /reminders` is the one exception;
  it is a read across every memo by a caller holding none of them.
- **Lists echo the filters they answered for** alongside the rows, so a response
  that arrives after the search box has moved on can still be captioned correctly.
- **`title` is the only content a client may write.** The transcript is a record of
  what was said and nothing in this app edits one — the formatter will not respell a
  word of it, and there is a test asserting so. A title is *generated*, which makes it
  a guess, and a guess the owner disagrees with is a memo they cannot find again. So
  the guess is the default and the owner has the last word.
- **A 5xx body is never shown to the user, whatever it says.** With `APP_DEBUG=false`
  it is `{"message":"Server Error"}`, which tells nobody anything; with it on, it is
  the exception and its trace. The frontend replaces both with a sentence naming the
  log, the same rule the worker applies to ffmpeg's stderr. A 4xx message *is* shown
  verbatim — those are written to be read.

## Architecture

_TODO (MEMO-26): short version here; the reasoning and trade-offs live in
NOTES.md (MEMO-27)._

## Assumptions

- Single user. No authentication, no multi-tenancy.
- Local-only. Not hardened for a public deployment.
- Audio is kept on a Docker volume, not object storage.

## Development

### Running the api's tests

`phpunit` is not in the `api` image — it runs `composer install --no-dev`, the same
way the `ai` image installs `requirements.txt` only — so `docker compose exec api
php artisan test` reports that there is no such command. Install the dev
dependencies into the checkout's `vendor/` first, then run the suite in the api
image so it has the right `php.ini` and the `pdo_pgsql` driver:

```bash
docker run --rm -v "$PWD/api":/app -w /app composer:2 composer install --no-interaction
```

```bash
docker compose run --rm --no-deps -v "$PWD/api/vendor":/app/vendor --entrypoint sh api -c 'php vendor/bin/phpunit'
```

**The `-v "$PWD/api/vendor":/app/vendor` is load-bearing, and leaving it off fails
in a way that reads as the install not having worked.** The `api` service declares
an anonymous volume over `/app/vendor` so the `./api` bind mount cannot shadow the
tree composer installed into the image — see the comment on it in
`docker-compose.yml`. That volume holds the image's `--no-dev` vendor, so without
this override the container answers `Could not open input file:
vendor/bin/phpunit` no matter what is on the host. Naming the host path explicitly
is what puts the dev tree back on top of it.

It also has to be the **api image** rather than a bare `php:8.3-cli`. Two tests fail
there, and both are the environment and not the code: the upload-size test reads
`post_max_size` from `conf.d/uploads.ini`, which only this image has, and the health
test expects a `SQLSTATE` in the message, which needs `pdo_pgsql` to be installed at
all.

No database is needed. The suite runs on sqlite in memory (`phpunit.xml`), and every
Postgres-specific statement sits behind a repository the tests substitute a fake for
— each `tests/Support/Fake*Repository.php` records what its real counterpart was
verified to do against a live Postgres, and why that could not be faked convincingly
here.

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

ffmpeg _is_ needed by `tests/test_audio.py`, which normalizes real files rather
than mocking the transcode. Run from the `ai` image as above it is present; run on
a bare host without it, those tests skip and the rest of the suite still passes.

`tests/test_fixtures.py` is the three-browser acceptance check and needs genuine
recordings in `ai/tests/fixtures/`, since no synthesized file reproduces the
missing-duration defect. It skips and names what is missing until they are there —
`ai/tests/fixtures/README.md` has the capture instructions.

`tests/test_local_whisper.py` runs the real model against those same recordings,
and `tests/test_baked_models.py` checks the weights are where the image put them.
Both run as part of the command above with nothing extra mounted, because the
weights are in the image — the `-v memo-app_whisper-cache:/cache` this section used
to ask for is no longer needed and the volume it named no longer exists.

They still skip on a bare host with no image, and they will not download anything
to avoid it — a test run that quietly pulled 1.6 GB would be a worse surprise than
a skip.

Everything else about the local provider is covered by `tests/test_local_stt.py`,
which stubs the model out: what it checks is the classification — which failures
send the chain to the fallback and which are terminal — and none of that needs
inference.

_TODO (MEMO-26): running the api tests, running a service outside Docker, applying
a new migration._
