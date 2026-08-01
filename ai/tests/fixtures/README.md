# Real browser recordings

`tests/test_fixtures.py` is MEMO-13's acceptance criterion: feed one recording
from each of Chrome, Safari and Firefox through normalization and check that all
three produce a playable 16 kHz mono file and a correct duration.

It needs **genuine MediaRecorder output**, and a file synthesized with ffmpeg
will not do. The specific defect the test exists to prove is handled is that
MediaRecorder writes its container to a sink it cannot seek back into, so it
never returns to fill in the duration — a Chrome recording arrives with no
Duration element in the Segment Info and no Cues, and `ffprobe` answers `N/A`
rather than a number. Only real browser output reproduces that reliably.

`tests/test_audio.py` approximates it by piping ffmpeg's output, which produces
the same missing field, and that covers the mechanics. What it cannot cover is
each browser's actual container quirks — which is the whole of what these three
files are for.

## What to put here

Three short clips, a few seconds each, named by the browser that produced them:

| File | Browser | Container it will actually be |
| --- | --- | --- |
| `chrome.webm` | Chrome or Edge | WebM/Opus, sniffs as `video/webm` |
| `firefox.ogg` | Firefox | Ogg/Opus — Firefox reports `audio/webm` and produces Ogg ([bug 1501308](https://bugzilla.mozilla.org/show_bug.cgi?id=1501308)) |
| `safari.mp4` | Safari | MP4/AAC, sniffs as `video/mp4` |

The extension is matched loosely — the test globs on the stem, so
`firefox.webm` is found too if that is what the download is called. Two of the
three containers sniff as `video/…`; that is expected and
`App\Http\Rules\SniffedAudioType` has the reasoning.

## How to capture them

Easiest is through the app itself, since it is the code path that matters:

1. `docker compose up`, open the app in the browser in question, record a few
   seconds, press **Stop**.
2. The blob is uploaded and stored under the `audio` volume. Copy it out:

```bash
docker compose exec api sh -c 'ls -t $(find /data/audio -type f) | head -1'
```

then

```bash
docker compose cp api:/data/audio/<the path that printed> ai/tests/fixtures/chrome.webm
```

Recording straight from a devtools console with `MediaRecorder` and saving the
blob works just as well — the point is only that a browser wrote the container.

Safari needs a secure context, so use `http://localhost:5173` rather than a LAN
address; the README's Recording section covers that.

## These files are committed, despite the audio rules in `.gitignore`

`.gitignore` drops `*.webm`, `*.ogg`, `*.m4a`, `*.wav` and `*.mp3` so that a
bare-metal run cannot commit uploaded audio into the repo. That pattern also
matched this directory, which meant `git add` here reported nothing and the files
never arrived — passing locally for whoever captured them and skipping forever on
a clean clone. There are `!ai/tests/fixtures/*` negations for exactly that, with
the reasoning at the rules. Safari's `.mp4` was never matched and needs none.

## Until they are here

`tests/test_fixtures.py` skips **per browser**, so whichever recordings exist are
asserted against and the skip names only what is still missing. The rest of the
suite is unaffected, so a stranger cloning the repo still gets a green run.

## Two questions MEMO-11 left for these files

Both are about bitrate, and neither is answerable without real recordings from
all three browsers. MEMO-11 measured **Chromium only**.

1. **Do Firefox and Safari honour `audioBitsPerSecond`?** The recorder asks for
   48 kbps (`web/src/composables/useRecorder.js`). Chromium obliges — 49 kbps on
   the wire, against 153 kbps if left to its own default. The spec says the other
   two should, but neither has been run. Divide a fixture's size by the duration
   this pipeline now measures and the answer falls out.

2. **Should `MAX_AUDIO_BYTES` go up?** It exists to bound an upload; the duration
   cap is what is meant to stop a long memo. At 48 kbps, 12 MiB is about 34
   minutes, so the two caps are comfortably separated and the answer today is no.
   That separation is what question 1 puts at risk: a browser that ignores the
   hint and records at 153 kbps puts a ten-minute memo at 11.5 MB against a 12.6
   MB cap, and then a memo inside the documented duration can be refused as too
   large instead — the wrong error, on a variable-bitrate codec, so which one
   fires depends on how much of the recording was silence.
