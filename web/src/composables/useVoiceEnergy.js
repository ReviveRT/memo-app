import { ref } from 'vue'

/*
 * How loud, how fast, and where the syllables land. Three numbers off the
 * recording stream, for the background to move to.
 *
 * The state is at module scope, which is the opposite of useRecorder's choice and
 * for the reason useMemos gives for its own: two components share this. The
 * microphone is opened by MemoRecorder and the thing that moves to it is
 * MemoBackdrop, which sits behind the whole page in App.vue -- so a per-caller
 * copy would leave the backdrop reading an analyser nobody had attached a stream
 * to. It is one meter, the way there is one memo list.
 *
 * What this file deliberately does not own is the microphone. useRecorder opens
 * the stream, decides when it is over, and calls attach/detach; nothing here
 * stops a track or holds a MediaStream past detach(). That split is why the
 * recording indicator's lifetime stays readable in one file.
 */

/**
 * Whether a stream is currently attached.
 *
 * The only reactive value here, and it changes twice per recording. Everything
 * else is read from sample() inside a requestAnimationFrame callback, so making
 * the levels refs would push sixty reactive invalidations a second through Vue to
 * move numbers that nothing but a canvas ever reads.
 *
 * @type {import('vue').Ref<boolean>}
 */
const active = ref(false)

/** @type {?AudioContext} */
let audioCtx = null

/** @type {?AnalyserNode} */
let analyser = null

/** @type {?Float32Array} */
let frames = null

/**
 * The window the analyser hands back, in samples.
 *
 * 1024 at 48 kHz is about 21 ms -- short enough that a syllable's attack is not
 * averaged away, long enough that the RMS of a low voice is not dominated by
 * whichever part of the waveform the window happened to land on.
 */
const FFT_SIZE = 1024

/**
 * The loudness window, in dBFS.
 *
 * -60 is quiet room noise and maps to nothing happening; -12 is a raised voice
 * close to the microphone and maps to fully open. Speech at a normal distance
 * sits around -30 to -20, which is deliberately in the middle of the range
 * rather than at the top: the effect has somewhere to go when somebody leans in.
 */
const MIN_DB = -60
const MAX_DB = -12

/**
 * The envelope, in seconds. Fast attack, slow release.
 *
 * This asymmetry is doing most of the work in how the effect *feels*, and it is
 * borrowed from audio metering rather than invented: a symmetric filter fast
 * enough to catch a syllable also drops out between syllables, so the cloud
 * flickers; one slow enough not to flicker arrives visibly after the sound. Rise
 * on the fast constant and fall on the slow one and it snaps to speech and then
 * glides, which is what a voice looks like.
 */
const ATTACK = 0.06
const RELEASE = 0.35

/**
 * A second, much faster envelope -- the one the onsets are counted off.
 *
 * Two envelopes rather than one, and this is the correction to a version that had
 * only the slow pair above. Measured against synthetic speech at a known rate,
 * that version reported 1.57 syllables/s for a 2/s input, 2.04 for 4/s and 1.50
 * for 6/s: it rose and then *fell*, so the effect ran slower the faster you spoke
 * past about four syllables a second. The cause is RELEASE. At 6/s the gaps
 * between syllables are ~70 ms and a 350 ms release cannot fall through them, so
 * the envelope flattens into a plateau, the baseline rises to meet it, and no
 * peak ever clears the threshold again.
 *
 * The two envelopes want opposite things and cannot be one number. The visual one
 * has to glide, or the cloud flickers on every syllable. The detection one has to
 * dip between syllables, or there is nothing to detect. At a 50 ms release the
 * fast envelope falls to about a quarter of its peak across a 70 ms gap, which is
 * ample.
 */
const DETECT_ATTACK = 0.005
const DETECT_RELEASE = 0.05

/**
 * The shortest gap between two onsets, in seconds.
 *
 * The fastest anybody articulates is around eight syllables a second, so a second
 * peak inside 90 ms is the same syllable's waveform crossing the threshold twice
 * rather than a new one. Left at the 120 ms this started as, the ceiling would sit
 * at 8.3/s -- close enough to the fastest real speech that the refractory itself
 * would start capping the reading.
 */
const REFRACTORY = 0.09

/** How much recent history the rate is counted over, in seconds. */
const RATE_WINDOW = 2

/** How quickly the reported rate follows the counted one, in seconds. */
const RATE_TAU = 0.6

/**
 * How far above its own baseline the fast envelope has to rise to count as an
 * onset, and how far back below it has to fall before another one can fire.
 *
 * Against a slow-moving baseline rather than a fixed threshold, so this works the
 * same for somebody speaking quietly and somebody shouting -- the baseline
 * follows the voice and the test is always "louder than you have just been". The
 * lower re-arm value is hysteresis: with a single threshold, an envelope sitting
 * near it fires on every ripple.
 */
const ONSET_RISE = 0.08
const ONSET_REARM = 0.03

/**
 * How quickly the baseline follows the fast envelope, in seconds.
 *
 * It has to be slower than a syllable, or it tracks each one and nothing ever
 * clears it; and faster than somebody changing how loudly they are speaking, or
 * the detector goes deaf for a second whenever they do. Between about 0.15 and 2
 * seconds, so a third of a second sits comfortably in the middle rather than at
 * either end.
 */
const BASELINE_TAU = 0.35

/** Below this, an onset is noise in a silent room rather than a syllable. */
const ONSET_FLOOR = 0.06

/**
 * The spring a syllable kicks: its frequency in Hz, and its damping as a
 * fraction of critical.
 *
 * Slightly under-damped on purpose. Critically damped is the safe choice and it
 * reads as inflation -- the cloud gets bigger and then smaller. A little bounce
 * reads as an impact, which is what a syllable is.
 */
const KICK_HZ = 3.2
const KICK_DAMPING = 0.72

/**
 * Peak displacement of this spring per unit of velocity impulse, in units of
 * 1/omega.
 *
 * Here because the obvious `kickV += 1` is wrong by a factor of omega, which is
 * not visible as a bug so much as an absence: measured, a syllable moved the
 * spring 0.016 when the renderer was scaling it as though it moved about 1, so the
 * per-syllable beat -- the whole reason there is a spring rather than another
 * envelope -- was invisible. An impulse in *velocity* produces a displacement of
 * roughly v/omega, and omega here is 20 rad/s.
 *
 * Derived rather than tuned, so that changing KICK_DAMPING cannot silently change
 * how hard a syllable hits. For x'' = -w^2 x - 2*z*w*x' driven by x'(0) = v:
 *
 *   x(t)  = v / (w*wd) * exp(-z*w*t) * sin(w*wd*t),  wd = sqrt(1 - z^2)
 *   t_peak = atan2(wd, z) / (w*wd)
 *
 * which makes the bracketed quantity below x_peak * w / v -- so an impulse of
 * `KICK_PEAK * w / KICK_RESPONSE` lands the peak on KICK_PEAK. At z = 0.72 it
 * comes out at 0.451, and the impulse at about 44.6.
 */
const KICK_RESPONSE = (() => {
  const wd = Math.sqrt(1 - KICK_DAMPING * KICK_DAMPING)
  const peak = Math.atan2(wd, KICK_DAMPING) / wd

  return (Math.exp(-KICK_DAMPING * peak) * Math.sin(wd * peak)) / wd
})()

/** What one syllable in isolation should move the spring to. */
const KICK_PEAK = 1

/**
 * The ceiling on the kick.
 *
 * Successive syllables add impulses before the previous one has decayed, and near
 * the spring's own 3.2 Hz that accumulation is constructive -- which is wanted,
 * since a fast run of syllables should read as more excited than one. Unbounded it
 * is not: the renderer turns this straight into size, so a cap is what keeps a
 * loud fast passage from inflating the field without limit. Above the single-hit
 * peak, so a run still visibly beats harder than one syllable.
 */
const KICK_CEILING = 1.5

/* The meter's own state. Reset by attach, decayed to nothing by sample when
 * there is no stream. */
let level = 0
let detect = 0
let rate = 0
let baseline = 0
let armed = true
let kickX = 0
let kickV = 0

/** Onset times in seconds on this meter's own clock, oldest first. */
let onsets = []

/** Seconds since the meter started, accumulated from the frame deltas. */
let clock = 0

/** The reading handed back by sample(), reused rather than reallocated per frame. */
const reading = { level: 0, rate: 0, kick: 0 }

/**
 * A one-pole low-pass step that is correct for a variable frame interval.
 *
 * `current + (target - current) * k` with a constant k is the usual shape and it
 * is wrong here: k would mean something different at 60 Hz than at 120, so the
 * envelope's timing would depend on the monitor. Expressed as a time constant it
 * does not.
 */
function onePole(current, target, tau, dt) {
  return current + (target - current) * (1 - Math.exp(-dt / Math.max(tau, 1e-4)))
}

/**
 * Start metering a stream. Called by useRecorder once it has one.
 *
 * Never throws: this is decoration, and a browser with no AudioContext, or one
 * that refuses to build the graph, should cost a still background rather than a
 * recording. Returns whether it worked, for a caller that wants to know.
 */
function attach(stream) {
  detach()

  const Ctor = window.AudioContext ?? window.webkitAudioContext

  if (!Ctor || !stream) {
    return false
  }

  try {
    audioCtx = new Ctor()
    analyser = audioCtx.createAnalyser()
    analyser.fftSize = FFT_SIZE

    // Checked here rather than discovered in measure(). It is present everywhere
    // this app is likely to run, but the shape of the failure if it is not decides
    // that this line is worth having: measure() is called from inside a
    // requestAnimationFrame loop, so a missing method would throw sixty times a
    // second forever instead of once. Thrown into the catch below, which is the
    // path that leaves the background still and the recording working.
    if (typeof analyser.getFloatTimeDomainData !== 'function') {
      throw new TypeError('AnalyserNode has no getFloatTimeDomainData')
    }

    // Zero, because the smoothing that belongs here is the envelope above --
    // which is asymmetric, and this one is not. Left at its 0.8 default the two
    // filters fight and the attack is no longer the attack this file documents.
    analyser.smoothingTimeConstant = 0

    // Connected to the analyser and stopped there. Carrying on to
    // audioCtx.destination is what an audio graph usually wants and here it is a
    // feedback loop: the microphone's own signal out of the speakers, back in.
    audioCtx.createMediaStreamSource(stream).connect(analyser)

    frames = new Float32Array(analyser.fftSize)
  } catch {
    detach()

    return false
  }

  // Suspended is the autoplay policy's doing. It should not happen here -- this
  // runs after a click, by way of getUserMedia -- but resuming costs one call and
  // the failure it prevents is an analyser that reads silence forever.
  audioCtx.resume?.().catch(() => {})

  level = 0
  rate = 0
  baseline = 0
  armed = true
  kickX = 0
  kickV = 0
  onsets = []
  active.value = true

  return true
}

/**
 * Stop metering and let go of the audio graph.
 *
 * Idempotent, and it closes the AudioContext rather than leaving it suspended: an
 * open context holds a hardware audio callback, and on some platforms that is
 * enough for the OS to keep listing the tab as using the microphone after the
 * recording has ended. The levels are left where they are -- sample() eases them
 * down, so the cloud settles instead of collapsing on the frame Stop was pressed.
 */
function detach() {
  active.value = false
  analyser = null
  frames = null

  // Cleared here rather than on the next sample(), so that "no stream" is a state
  // this function fully establishes. The reported rate is not zeroed with it -- it
  // eases down, because onsets is the rate's *input* and sample() filters toward
  // it, which is what makes releasing the microphone a settle rather than a jump.
  onsets = []
  armed = true

  const closing = audioCtx

  audioCtx = null
  closing?.close?.().catch(() => {})
}

/**
 * The current loudness in the normalized range, straight off the analyser.
 *
 * RMS rather than the peak of the byte-domain data: peak is one sample and jumps
 * around, and this number is about how loud a voice is rather than how tall its
 * tallest excursion was.
 */
function measure() {
  analyser.getFloatTimeDomainData(frames)

  let sum = 0

  for (let i = 0; i < frames.length; i++) {
    sum += frames[i] * frames[i]
  }

  const db = 20 * Math.log10(Math.sqrt(sum / frames.length) + 1e-9)

  return Math.min(1, Math.max(0, (db - MIN_DB) / (MAX_DB - MIN_DB)))
}

/**
 * Advance the meter by one frame and read it.
 *
 * `sample(0)` is a pure read, and two callers depend on that -- MemoBackdrop draws
 * its single reduced-motion frame with it. That has to be arranged rather than
 * assumed: every filter below is a no-op at dt 0 by construction, but onset
 * detection is a threshold test on the *current* value and would happily fire on a
 * read, adding a syllable nobody spoke and kicking the spring. Hence the guard.
 *
 * @param {number} dt Seconds since the last call. Zero to read without advancing.
 * @returns {{level: number, rate: number, kick: number}} The same object every
 *   time, mutated in place. Read it, do not keep it.
 */
function sample(dt) {
  clock += dt

  if (analyser === null) {
    // No stream. Ease everything back to idle rather than zeroing it, so that
    // releasing the microphone is a settle rather than a jump.
    level = onePole(level, 0, RELEASE, dt)
    detect = 0
    rate = onePole(rate, 0, RATE_TAU, dt)
    baseline = 0
  } else {
    const target = measure()

    level = onePole(level, target, target > level ? ATTACK : RELEASE, dt)
    detect = onePole(detect, target, target > detect ? DETECT_ATTACK : DETECT_RELEASE, dt)
    baseline = onePole(baseline, detect, BASELINE_TAU, dt)

    if (dt > 0) {
      const last = onsets.length > 0 ? onsets[onsets.length - 1] : -Infinity

      if (armed && detect > baseline + ONSET_RISE && detect > ONSET_FLOOR && clock - last > REFRACTORY) {
        onsets.push(clock)
        armed = false
        kickV += (KICK_PEAK / KICK_RESPONSE) * 2 * Math.PI * KICK_HZ
      } else if (!armed && detect < baseline + ONSET_REARM) {
        armed = true
      }

      while (onsets.length > 0 && clock - onsets[0] > RATE_WINDOW) {
        onsets.shift()
      }
    }

    rate = onePole(rate, onsets.length / RATE_WINDOW, RATE_TAU, dt)
  }

  // Semi-implicit Euler, which is enough for a spring nobody is going to
  // integrate for more than a few seconds at a time, and the frame delta the
  // caller passes is clamped so a backgrounded tab cannot hand it a step large
  // enough to make this diverge.
  const w = 2 * Math.PI * KICK_HZ
  const acceleration = -(w * w) * kickX - 2 * KICK_DAMPING * w * kickV

  kickV += acceleration * dt
  kickX += kickV * dt

  reading.level = level
  reading.rate = rate
  reading.kick = Math.min(KICK_CEILING, Math.max(0, kickX))

  return reading
}

/**
 * The meter.
 *
 * @returns {{
 *   active: import('vue').Ref<boolean>,
 *   attach: (stream: MediaStream) => boolean,
 *   detach: () => void,
 *   sample: (dt: number) => {level: number, rate: number, kick: number},
 * }}
 */
export function useVoiceEnergy() {
  return { active, attach, detach, sample }
}
