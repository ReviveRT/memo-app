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
| `AUDIO_DIR` | `/data/audio` | Audio path inside the containers, on the shared `audio` volume. Changing it needs a rebuild with a matching `--build-arg AUDIO_DIR` — see the note in `.env.example` |

### Transcription

Recordings are transcribed **on your machine**, by
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) running inside the
`ai-worker` container. There is no account to create, no key to paste and no
per-minute bill; the only thing it spends is CPU. The weights are MIT-licensed and
so is everything that runs them.

The one moment it needs the internet is **the first run after a clean build**,
which downloads 1.6 GB of model into the `whisper-cache` volume. The worker starts
that download the moment it boots rather than waiting for your first recording, so
in practice it is finished before you have opened the browser and pressed Record.
If you beat it, that memo fails with _"the local transcription model is still
being downloaded"_ — record another a minute later. Everything after that is
offline, and stays offline across restarts because the cache is a named volume.

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
one comma in them**, while every short memo from the same session was punctuated
correctly:

> database is running from yet another work free and nothing is running from main
> also that earlier 200 i trusted was misleading let me see what it actually
> returns and critically confirms your 38 memos are …

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
transcribe, store and search; they get a fallback title (first 60 characters of
the transcript) and `enrichment_error` is recorded on the row.

To use it, paste your own key into `.env`. _TODO (MEMO-26): what measurably
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

ffmpeg _is_ needed by `tests/test_audio.py`, which normalizes real files rather
than mocking the transcode. Run from the `ai` image as above it is present; run on
a bare host without it, those tests skip and the rest of the suite still passes.

`tests/test_fixtures.py` is the three-browser acceptance check and needs genuine
recordings in `ai/tests/fixtures/`, since no synthesized file reproduces the
missing-duration defect. It skips and names what is missing until they are there —
`ai/tests/fixtures/README.md` has the capture instructions.

`tests/test_local_whisper.py` runs the real model against those same recordings.
It skips unless the weights are already in the HuggingFace cache, and it will not
download them itself — a test run that quietly pulled 145 MB would be a worse
surprise than a skip. Mount the same cache the stack uses to make it run:

```bash
docker compose run --rm --no-deps --user 0:0 -v memo-app_whisper-cache:/cache --entrypoint sh ai-worker -c 'pip install -q -r requirements-dev.txt && python -m pytest'
```

Everything else about the local provider is covered by `tests/test_local_stt.py`,
which stubs the model out: what it checks is the classification — which failures
send the chain to the fallback and which are terminal — and none of that needs
inference.

_TODO (MEMO-26): running the api tests, running a service outside Docker, applying
a new migration._
