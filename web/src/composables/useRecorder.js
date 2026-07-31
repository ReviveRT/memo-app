import { onUnmounted, ref } from 'vue'

/*
 * The microphone, and nothing about memos.
 *
 * State is declared inside the function rather than at module scope, which is the
 * opposite of useMemos and for the reason that file gives for its own choice: the memo
 * list is shared by three components and has to be one array, while a recorder belongs
 * to the one component that draws the button. Module scope here would make a second
 * recorder on the page silently share a MediaRecorder with the first, and would keep a
 * live MediaStream alive across an HMR reload.
 *
 * What this file deliberately does not do is negotiate a container. See CONTAINERS
 * below.
 */

/**
 * How often the browser hands over a chunk while recording.
 *
 * `start()` with no argument is the simpler call and produces one blob at the end;
 * this is the more defensive of the two. WebM, Ogg and Safari's fragmented MP4 are all
 * built to be concatenated, so assembling the chunks costs nothing, and it means a long
 * recording is a list of ~1s pieces rather than one allocation that grows for ten
 * minutes.
 *
 * Stated plainly because this task's 🙋 callout is about exactly this. What was actually
 * run: Chrome, against a synthetic MediaStream from an AudioContext rather than a
 * microphone, which produced a 144 KB WebM the API accepted and the worker transcribed.
 * Firefox's and Safari's containers were exercised only from the server side, as bytes
 * posted by hand -- no Firefox or Safari MediaRecorder has run this code. So the
 * concatenation claim above is verified for WebM and assumed for Ogg and fragmented MP4,
 * and it is one of the things Pavel's pass over the three browsers is for.
 */
export const TIMESLICE_MS = 1_000

/**
 * How often the elapsed display is recomputed. Five times a second, so the seconds
 * digit turns over when it should rather than up to a second late.
 */
const TICK_MS = 200

/*
 * CONTAINERS
 *
 * `new MediaRecorder(stream)` with no options, on purpose, and this is the one part of
 * this file worth reading before changing it.
 *
 * Every browser produces a different container -- Chrome and Edge WebM/Opus, Safari
 * MP4 or fragmented MP4, Firefox **Ogg** -- and asking for one does not change that.
 * Firefox returns true from `MediaRecorder.isTypeSupported('audio/webm;codecs=opus')`
 * and then produces Ogg anyway (Mozilla bug 1501308), so client-side negotiation cannot
 * even tell you what you got, let alone choose it.
 *
 * So there is nothing to negotiate: send whatever the browser produced and normalize it
 * server-side, which is MEMO-13's ffmpeg pass. The consequence to keep in mind is that
 * `mimeType` below is a label from the same browser that gets its own container wrong,
 * which is why the API sniffs the bytes rather than believing it
 * (App\Http\Requests\StoreMemoRequest::audio).
 */

/**
 * What to say when `navigator.mediaDevices` is not there at all.
 *
 * That is the shape an insecure origin takes: getUserMedia is not merely refused, the
 * whole `mediaDevices` object is undefined outside a secure context, so the naive call
 * is a TypeError about reading a property of undefined rather than anything about
 * microphones. Reachable by opening the app on a LAN IP instead of localhost, which is
 * the ordinary way to look at a dev server from a phone -- so this sentence is the one
 * that has to name the fix. The README says the same thing next to the URL.
 *
 * The port comes from the page rather than from the documented default of 5173, because
 * WEB_PORT is remappable and somebody who moved it is exactly the sort of person looking
 * at this app on a second machine. Told to open :5173 they would find nothing there,
 * which is a worse answer than the one they started with.
 *
 * The address they are *currently* on is deliberately not quoted back at them. It reads
 * well for the real case -- "rather than 192.168.1.5:5273" -- and it was tried, but the
 * only way to see this message during development is to remove `mediaDevices` by hand on
 * localhost, where the sentence then says to open localhost rather than localhost. A
 * message with a nonsense reading available to it is worse than one that leaves out
 * something the address bar is already showing.
 */
function insecureContextMessage() {
  const origin = window.location.port ? `localhost:${window.location.port}` : 'localhost'

  return (
    `Recording needs a secure context — open the app on http://${origin} rather than a ` +
    'LAN IP. Browsers only expose the microphone on localhost or over HTTPS.'
  )
}

/**
 * getUserMedia's rejections, in the words of somebody who wants to record a memo.
 *
 * Keyed by DOMException name rather than by message: the names are specified and the
 * messages are not ("Permission denied" in Chrome, "The request is not allowed by the
 * user agent or the platform in the current context." in Firefox, for one rejection).
 */
const FAILURES = {
  NotAllowedError:
    'Microphone access was blocked. Allow it for this site — the icon at the end of ' +
    'the address bar — and try again.',
  PermissionDeniedError: 'Microphone access was blocked. Allow it for this site and try again.',
  NotFoundError: 'No microphone was found. Check that one is connected and selected in your OS.',
  DevicesNotFoundError: 'No microphone was found. Check that one is connected.',
  NotReadableError:
    'The microphone could not be opened — another application may be holding it. Close ' +
    'anything else recording and try again.',
  TrackStartError: 'The microphone could not be opened — another application may be holding it.',
}

/**
 * The sentence for a rejection, or a last-resort one naming the rejection itself.
 *
 * SecurityError is routed to the secure-context message rather than listed above,
 * because it is the same problem in a different costume: where Chrome removes
 * `mediaDevices` from an insecure origin outright, other engines keep it and reject
 * with this. Both mean "open it on localhost", and neither is worth two wordings.
 */
function failureMessage(name) {
  if (name === 'SecurityError') {
    return insecureContextMessage()
  }

  return FAILURES[name] ?? `The microphone could not be opened (${name ?? 'unknown error'}).`
}

/**
 * Record, stop, and know how long it has been.
 *
 * @returns {{
 *   recording: import('vue').Ref<boolean>,
 *   elapsedMs: import('vue').Ref<number>,
 *   error: import('vue').Ref<?string>,
 *   start: () => Promise<boolean>,
 *   stop: () => Promise<?{blob: Blob, filename: string}>,
 *   discard: () => void,
 * }}
 */
export function useRecorder() {
  const recording = ref(false)
  const elapsedMs = ref(0)

  /** The last thing that went wrong, as a sentence to render. Cleared by the next start. */
  const error = ref(null)

  /** @type {?MediaRecorder} */
  let recorder = null

  /** @type {?MediaStream} */
  let stream = null

  /** @type {Array<Blob>} */
  let chunks = []

  /** The elapsed-display interval, or null when nothing is scheduled. */
  let ticker = null

  /**
   * The reading of performance.now() the recording began at.
   *
   * performance.now() and not Date.now(): this is a duration, and the wall clock can
   * move under it — an NTP correction or the user changing the time zone mid-recording
   * would otherwise show a memo as lasting minus three seconds.
   */
  let startedAt = 0

  /**
   * How the `stop` event gets back to whoever called stop(). Null while not stopping.
   *
   * A promise resolved from the event handler rather than an await on the event,
   * because the blob is only complete once the recorder has flushed: `stop()` returns
   * immediately and the last `dataavailable` arrives after it.
   *
   * @type {?(recorded: ?{blob: Blob, filename: string}) => void}
   */
  let settle = null

  /**
   * Whether the recorder reported an error during this recording.
   *
   * Kept as a flag rather than acted on where it happens, because the `error` event is
   * followed by `stop`, and the handler for that is the one place that knows whether
   * anyone is waiting for a blob.
   */
  let failed = false

  async function start() {
    if (recording.value) {
      return false
    }

    error.value = null
    chunks = []
    failed = false

    // Optional chaining on `mediaDevices` rather than on getUserMedia alone: outside a
    // secure context the property does not exist. See insecureContextMessage.
    if (!navigator.mediaDevices?.getUserMedia) {
      error.value = insecureContextMessage()

      return false
    }

    // A browser old enough to have getUserMedia and no MediaRecorder. Checked
    // separately so that case says what is missing instead of failing later with a
    // ReferenceError, and because the permission prompt below is worth not showing to
    // somebody who cannot record either way.
    if (typeof MediaRecorder === 'undefined') {
      error.value =
        'This browser cannot record audio. Type the memo instead, or use a newer browser.'

      return false
    }

    try {
      // `{ audio: true }` and no constraints. Sample rate, channel count and echo
      // cancellation are all things ffmpeg fixes for free in MEMO-13, and asking for
      // any of them here risks an OverconstrainedError on a device that would
      // otherwise have recorded perfectly well.
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (cause) {
      error.value = failureMessage(cause?.name)

      return false
    }

    try {
      recorder = new MediaRecorder(stream)
    } catch (cause) {
      // Reachable even with no options, on a platform whose default encoder is
      // unavailable. Releasing the stream matters here specifically: the permission
      // prompt has already been answered and the tracks are live, so leaving them open
      // means the browser's recording indicator stays on for a recording that never
      // started.
      release()
      error.value = `This browser could not start a recording (${cause?.name ?? 'unknown error'}).`

      return false
    }

    recorder.addEventListener('dataavailable', (event) => {
      // Zero-length chunks are normal -- a timeslice that elapses with nothing buffered
      // produces one -- and pushing them would put empty blobs in the middle of the
      // container.
      if (event.data?.size > 0) {
        chunks.push(event.data)
      }
    })

    recorder.addEventListener('error', (event) => {
      failed = true
      error.value = `Recording stopped unexpectedly (${event.error?.name ?? 'unknown error'}).`
    })

    // Bound to the instance rather than to `recorder`, so a `stop` event that arrives
    // after this recorder has been replaced cannot act on its successor. See finish().
    const instance = recorder

    recorder.addEventListener('stop', () => finish(instance))

    try {
      recorder.start(TIMESLICE_MS)
    } catch (cause) {
      // Separate from the constructor's try above, because a platform can accept the
      // recorder and refuse to start it. Without this the throw escapes into the click
      // handler as an unhandled rejection, leaving the button on "Record" and the
      // microphone open with nothing on screen saying anything happened.
      release()
      error.value = `Recording could not be started (${cause?.name ?? 'unknown error'}).`

      return false
    }

    recording.value = true
    startedAt = performance.now()
    elapsedMs.value = 0

    // Recomputed from the clock rather than accumulated by adding TICK_MS. Timers are
    // throttled to roughly once a second in a background tab, so counting ticks would
    // under-report a recording made while the user was looking at something else --
    // and the number's whole purpose is to be right about the length before sending.
    ticker = setInterval(() => {
      elapsedMs.value = performance.now() - startedAt
    }, TICK_MS)

    return true
  }

  /**
   * Answer whoever is awaiting stop(), exactly once, and forget them.
   *
   * Every exit from a recording goes through here rather than calling `settle` directly,
   * because the failure it prevents is invisible: a path that returns without settling
   * leaves the awaiting async function suspended for the life of the page, and from the
   * screen that is indistinguishable from a Stop button that did nothing. There are
   * three such exits -- the recording finishing, discard(), and unmount -- and the last
   * two were each missing it.
   */
  function answer(recorded) {
    const settled = settle

    settle = null
    settled?.(recorded)
  }

  /**
   * Stop, and resolve with the recording once the browser has flushed it.
   *
   * Resolves null rather than rejecting for every outcome that is not a recording:
   * nothing was running, a stop is already in progress, the recorder errored, the result
   * was zero bytes, or it was discarded out from under this call. A microphone that
   * produced silence still produces a container, so an empty blob means no audio reached
   * the browser at all -- which finish() reports in words rather than leaving to be
   * inferred from a button that did nothing.
   */
  function stop() {
    if (!recording.value || recorder === null) {
      return Promise.resolve(null)
    }

    // Already stopping. Without this a second click overwrites `settle` with the second
    // caller's resolve, and the first promise never settles at all -- an await in the
    // component that hangs for the lifetime of the page. Answering null is right rather
    // than merely safe: only one of the two callers can be handed the recording, and it
    // is the one that asked first.
    if (settle !== null) {
      return Promise.resolve(null)
    }

    return new Promise((resolve) => {
      settle = resolve

      // `inactive` if the recorder already stopped on its own -- the device was
      // unplugged, or the permission was revoked from the address bar mid-recording.
      // Calling stop() on it throws InvalidStateError, so finish() is called directly
      // and does the same cleanup the event would have triggered.
      if (recorder.state === 'inactive') {
        finish(recorder)
      } else {
        recorder.stop()
      }
    })
  }

  /** Throw the recording away and let go of the microphone. */
  function discard() {
    const instance = recorder

    if (recording.value && instance?.state !== 'inactive') {
      instance?.stop()
    }

    chunks = []
    elapsedMs.value = 0
    recording.value = false
    stopTicking()

    // Released here rather than left to the `stop` event, and this is the fix for two
    // faults rather than tidiness. The small one is that the microphone stays open --
    // and the OS keeps saying so -- for as long as the browser takes to finalize a
    // container nobody is going to read.
    //
    // The real one is that this method re-enables the Record button *synchronously*
    // while the event is still queued. Start another recording inside that window and
    // the late event ran finish() against whatever `recorder` pointed at by then --
    // which was the new recorder -- releasing its stream and stopping its timer, so the
    // recording that had just started silently died. Releasing now means the late event
    // finds an instance that is no longer current, and finish() drops it.
    release()

    // Both buttons are on screen together, so Discard is reachable while a stop() is
    // still waiting for its blob. That call is now never going to be answered by
    // finish() -- release() above made its event a no-op -- so it is answered here.
    answer(null)
  }

  /**
   * The `stop` event: assemble the chunks, release the microphone, answer the caller.
   *
   * Exactly one case reaches the body with no caller waiting: a recording the browser
   * ended by itself. discard() and unmount both release before their event lands, so the
   * instance guard below turns those into no-ops -- which is what the last branch relies
   * on when it treats `settle === null` as "this stopped on its own".
   *
   * @param {MediaRecorder} instance The recorder this call is about. Anything else is a
   *   `stop` event from a recorder that has since been replaced or released, and acting
   *   on it would apply one recording's ending to a different recording.
   */
  function finish(instance) {
    if (instance !== recorder) {
      return
    }

    stopTicking()
    recording.value = false

    const type = instance.mimeType || chunks[0]?.type || ''

    // The type is carried on the Blob as well as being used for the filename, because
    // it is what fetch puts in the part's Content-Type. The API does not believe it --
    // it sniffs -- but a multipart part labelled application/octet-stream when the
    // browser knew better is a worse request to read in a network log.
    const blob = new Blob(chunks, { type })

    chunks = []
    release()

    if (failed) {
      answer(null)

      return
    }

    // Zero bytes with nothing else wrong: a track that ended before it produced a
    // sample, or a device that was open but silent in the way a disconnected input is.
    // It needs its own sentence, because returning null on its own means the Stop button
    // does nothing at all and says nothing about why -- the silent failure this whole
    // file is written against.
    if (blob.size === 0) {
      error.value = 'That recording came back empty — no audio reached the browser.'
      answer(null)

      return
    }

    // A real recording with nobody waiting for it, which by elimination means the
    // browser ended this one itself: the device was unplugged, or the permission was
    // revoked from the address bar mid-recording. discard() and unmount both release
    // first, so their late events are stopped by the instance guard above and never
    // arrive here.
    //
    // The blob is lost either way -- the UI left the recording state the moment this
    // ran, and there is nowhere to put it -- but it must not be lost in silence, which
    // is a Record button that returns to idle mid-sentence and explains nothing.
    if (settle === null) {
      error.value =
        'Recording stopped on its own — the microphone was disconnected or its permission ' +
        'was withdrawn. Nothing was saved.'
    }

    answer({ blob, filename: `memo.${extensionFor(type)}` })
  }

  /**
   * Stop every track, which is what turns the browser's recording indicator off.
   *
   * Not optional politeness: a MediaStream left open holds the microphone for the whole
   * tab session, and on most platforms the OS shows the app as listening the entire
   * time.
   */
  function release() {
    stream?.getTracks().forEach((track) => track.stop())
    stream = null
    recorder = null
  }

  function stopTicking() {
    if (ticker !== null) {
      clearInterval(ticker)
      ticker = null
    }
  }

  // Vite's HMR re-runs setup on every edit to a component using this, and the tab is
  // holding a live microphone. Without this, each edit during development leaves
  // another open stream and another interval behind.
  onUnmounted(() => {
    if (recorder?.state !== 'inactive') {
      recorder?.stop()
    }

    stopTicking()
    release()

    // Same reasoning as discard(): release() has already made the queued `stop` event a
    // no-op, so a stop() that was in flight when the component went away is answered
    // here or never.
    answer(null)
  })

  return { recording, elapsedMs, error, start, stop, discard }
}

/**
 * A file extension for a MediaRecorder MIME type: `audio/webm;codecs=opus` -> `webm`.
 *
 * Only ever a filename. The API derives the storage key's extension from the sniffed
 * type instead, precisely because this one comes from the browser -- so a wrong answer
 * here is a confusing name in a network log rather than a mislabelled blob on the
 * volume.
 *
 * The fallback is `bin` rather than `webm`, so an unrecognised type is visibly
 * unrecognised instead of claiming to be the container Chrome happens to use.
 */
function extensionFor(mimeType) {
  const subtype = mimeType.split(';')[0].split('/')[1]?.toLowerCase() ?? ''

  return /^[a-z0-9]{1,8}$/.test(subtype) ? subtype : 'bin'
}
