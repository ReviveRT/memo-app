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

## Two questions MEMO-11 left for these files — now answered

Both were about bitrate, and neither was answerable without real recordings from
all three browsers. MEMO-11 had measured **Chromium only**. Measured off these
fixtures, dividing each file's size by the duration this pipeline recovers:

| | bytes | duration | effective |
| --- | --- | --- | --- |
| `chrome.webm` | 30,693 | 5.29 s | **46.4 kbps** |
| `firefox.ogg` | 29,915 | 4.95 s | **48.3 kbps** |
| `safari.mp4` | 34,691 | 5.40 s | **51.8 kbps** |

1. **Do Firefox and Safari honour `audioBitsPerSecond`?** Yes. All three land
   within a few kbps of the 48 requested in
   `web/src/composables/useRecorder.js`, against the 153 kbps Chromium produces
   when left to its own default. No browser ignores the hint.

2. **Should `MAX_AUDIO_BYTES` go up?** No, and question 1 is why. At ~48 kbps,
   12 MiB is about 34 minutes against a 10-minute duration cap, so the two limits
   stay comfortably separated on every browser. The failure mode MEMO-11 worried
   about — a browser recording at 153 kbps, putting a ten-minute memo at 11.5 MB
   against a 12.6 MB cap, so that a memo inside the documented duration gets
   refused as too *large* — does not occur.

Both figures are small samples of one recording each. They are decisive because
the gap being tested is threefold, not marginal.

## What these recordings showed that reasoning did not

**The missing duration is Chrome's, not MediaRecorder's.** Chrome's WebM reports
`format=duration` as `N/A`; Firefox's Ogg and Safari's MP4 both carry one. The
task was built on the general claim and the general claim is too strong.

It changes nothing about the design, for two reasons. Nothing upstream knows
which browser produced a given upload, so a duration read off the source is
trustworthy only two times in three and there is no way to tell which. And
Safari's own number disagrees with its audio: the container says 5.398667 s while
the decoded content is 5.359813 s, a 39 ms gap from AAC encoder delay. So the
source duration is either absent or slightly wrong, and normalizing first is what
makes it neither.
