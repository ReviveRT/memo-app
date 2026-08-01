<script setup>
import { onMounted, onUnmounted, useTemplateRef } from 'vue'
import { useVoiceEnergy } from '../composables/useVoiceEnergy'

/*
 * The background: a slow coloured bloom behind the page that breathes on its own
 * and moves to your voice while a memo is being recorded.
 *
 * Three stacked layers, all of them inert -- fixed, aria-hidden, pointer-events
 * none, and behind the content in z-order. Nothing on this page depends on any of
 * it, which is the property to preserve: every failure here has to end in a plain
 * dark page rather than in a memo that cannot be recorded.
 *
 * Why a canvas and not CSS keyframes, since that is the obvious first question:
 * the whole point of the effect is that its *rate* tracks how fast somebody is
 * speaking, and a keyframe animation cannot be retimed. Reassigning
 * animation-duration restarts the interpolation, so every change of speaking rate
 * would land as a visible jump. Here the frequencies are integrated into phases
 * (see advance) and a rate change is continuous by construction.
 *
 * Why not WebGL: a noise-warped field in a fragment shader would look slightly
 * better and costs a shader, a context-loss path and a fallback, in an app whose
 * package.json has one dependency. Measured through this file, timing the draw
 * call itself: **0.79 ms a frame** dark, 0.81 light, at a 317x179 backing store on
 * Chrome with --disable-gpu -- so a software rasteriser, and a pessimistic reading
 * against a GPU-backed canvas. About 5% of a 60 Hz frame budget, which is a
 * background's fair share and not the thing a shader would be bought to fix.
 *
 * That number is worth stating carefully because it was first measured wrong. An
 * earlier reading of 0.05 ms came from a headless run under
 * --virtual-time-budget, where performance.now() advances on virtual time and the
 * deltas around a synchronous call are not wall clock at all. Anything timed in
 * this app has to be timed without that flag.
 *
 * Timing the draw call alone also misses the compositor, which is where the cost
 * actually was: the softening blur used to be a CSS filter on the stretched
 * element, and end-to-end that halved the frame rate. It is now applied to the
 * small offscreen instead. See BLUR_CSS_PX, which has the measurements.
 */

/**
 * The id the bloom is centred on: the Record button, in MemoRecorder.
 *
 * Anchored to the control rather than to a fraction of the viewport so that the
 * light reads as coming off the button somebody is about to press, and so it
 * survives a reflow -- on a narrow window the column and the button move, and a
 * hardcoded 30% would not follow them.
 *
 * Two things fall out of the button's position that are worth knowing before
 * moving it. It sits at the left edge of a 44rem centred column, so on a wide
 * window the hot core lands in the left gutter, where no memo card can cover it
 * -- centred instead, the best part of the picture ends up hidden behind the
 * composer. And it is near the top of the page, so the bloom is cropped by the
 * top edge, which is deliberate: it reads as light spilling in from off-screen.
 */
const ANCHOR_ID = 'recorder-cloud-anchor'

/** Where to put the bloom when the button is not on the page. Matches its usual spot. */
const ANCHOR_FALLBACK = { x: 0.3, y: 0.14 }

/*
 * The field's shape and motion, shared by both colour schemes.
 *
 * One array rather than one per scheme, and that is the correction to a first pass
 * that duplicated all of this into a DARK and a LIGHT table. Nothing looked wrong,
 * because the two copies agreed -- but advance() below reads its frequencies by
 * index from a single table, so only one copy was ever driving the motion. Editing
 * a frequency in the other one would have changed nothing, silently. The schemes
 * differ in colour, not in how the field moves, so there is one table.
 *
 * `ring` is the radius fraction at which a colour is brightest, and it is the one
 * parameter that decides whether this looks like the reference or like a pile of
 * discs. The outer colours are haloes: violet peaking at 0.62 of its own radius
 * puts almost no blue in the middle, so the additive sum at the centre is red and
 * green only -- which is the hot yellow core. Built with the violet as a filled
 * disc instead, the centre washes out to white-pink and the whole thing reads as
 * a flashlight.
 *
 * `resp` is how much of the swell each blob takes. Above 1 for the inner ones, so
 * a loud syllable surges the core while the halo only swells: a field that scales
 * uniformly reads as a zoom rather than as something alive.
 */
const BLOBS = [
  { r: 0.62, ring: 0.62, w: 0.26, ax: 0.1, ay: 0.085, fx: 0.031, fy: 0.023, resp: 0.65 },
  { r: 0.5, ring: 0.5, w: 0.23, ax: 0.09, ay: 0.07, fx: 0.043, fy: 0.037, resp: 0.8 },
  { r: 0.38, ring: 0.4, w: 0.23, ax: 0.072, ay: 0.06, fx: 0.057, fy: 0.049, resp: 0.95 },
  { r: 0.3, ring: 0.28, w: 0.26, ax: 0.052, ay: 0.045, fx: 0.071, fy: 0.061, resp: 1.1 },
  { r: 0.22, ring: 0.1, w: 0.34, ax: 0.035, ay: 0.03, fx: 0.089, fy: 0.079, resp: 1.25 },
  { r: 0.15, ring: 0, w: 0.45, ax: 0.02, ay: 0.018, fx: 0.11, fy: 0.097, resp: 1.45 },
]

/** What each blob is painted with in the dark scheme, blob for blob. */
const DARK_INK = [
  { rgb: [140, 40, 235], a: 0.58 },
  { rgb: [255, 40, 150], a: 0.64 },
  { rgb: [255, 80, 120], a: 0.5 },
  { rgb: [255, 110, 20], a: 0.62 },
  { rgb: [255, 170, 30], a: 0.7 },
  { rgb: [255, 225, 130], a: 0.85 },
]

/*
 * The light scheme, which is a different compositing problem rather than the same
 * colours turned down. Additive blending onto a light page is white -- every blob
 * adds toward the background it is drawn on -- so these are pastel tints in
 * source-over, innermost drawn last.
 */
const LIGHT_INK = [
  { rgb: [178, 138, 255], a: 0.3 },
  { rgb: [255, 146, 198], a: 0.3 },
  { rgb: [255, 164, 168], a: 0.26 },
  { rgb: [255, 186, 130], a: 0.28 },
  { rgb: [255, 214, 146], a: 0.3 },
  { rgb: [255, 240, 200], a: 0.34 },
]

/**
 * How much larger and softer every blob is drawn in the light scheme.
 *
 * One factor rather than a second set of radii, which is what these were before the
 * tables were merged -- twelve numbers that differed from the dark ones by about
 * this much and could drift apart for no reason. Pastel tints need slightly more
 * spread than additive light to read as a wash rather than as shapes; that is the
 * whole of the difference.
 */
const LIGHT_SPREAD = 1.04
const LIGHT_WIDEN = 0.02

/**
 * Elongated lobes, rotating slowly, drawn under the round blobs.
 *
 * At this blur they are the faint radial streaks in the reference rather than
 * shapes -- they are what keeps the bloom from being perfectly concentric, which
 * is the thing that gives away a background made of circles.
 */
const PETALS = [
  { rgb: [255, 90, 200], r: 0.62, a: 0.3, ring: 0.42, w: 0.3, squash: 0.2, fr: 0.013, phase: 0 },
  { rgb: [255, 140, 60], r: 0.48, a: 0.26, ring: 0.3, w: 0.3, squash: 0.16, fr: -0.009, phase: 2.1 },
  { rgb: [190, 80, 255], r: 0.72, a: 0.22, ring: 0.55, w: 0.28, squash: 0.24, fr: 0.006, phase: 4.2 },
]

/** How many colour stops each blob's gradient gets. See gradientFor. */
const STOPS = 17

/**
 * The backing store, as a fraction of the viewport.
 *
 * The canvas is rendered at roughly a fifth of the size it is displayed at and
 * stretched by CSS, which is not a compromise but the reason this is cheap: the
 * upscale happens on the GPU during compositing, for nothing, and its bilinear
 * filtering is invisible on content that is nothing but smooth gradients. At 1440
 * by 900 that is a 317 by 198 canvas -- about a twentieth of the pixels, and the
 * per-frame cost quoted at the top of this file.
 */
const RESOLUTION = 0.22

/**
 * The softening blur, in *displayed* CSS pixels. Applied at backing-store scale.
 *
 * This is where the effect's real cost was, and the number that matters is not
 * this one but where the blur happens. Bilinear upscaling a 317px canvas to 1440
 * leaves a faint quilted texture in the mid-tones where it interpolates across a
 * curve, and about 18 displayed pixels of blur is where that stops being visible.
 *
 * Done as a CSS `filter` on the stretched element -- which is how this was built --
 * that is 18px over 1.17 million device pixels, re-rasterised every frame because
 * the canvas changes every frame. Measured on a software rasteriser by removing it
 * at runtime: 46 fps with, 78 fps without, and 76 fps with the whole canvas layer
 * hidden. So the blur cost more than everything else on the page put together, and
 * the drawing it was smoothing cost almost nothing.
 *
 * Applied here instead, to the offscreen as it is copied across, it is 4px over
 * 57 thousand pixels -- the same visual radius after the stretch, about a
 * twentieth of the work. Blurring before the upscale rather than after is not
 * identical in principle; compared side by side at this radius it is
 * indistinguishable, because both are smoothing the same absent detail.
 *
 * Two things this depends on. `ctx.filter` is unsupported on Safari before 17,
 * where the assignment is ignored and the result is an unblurred upscale -- mild
 * quilting, not a broken page. And the blur samples outside the canvas as
 * transparent, fading the outermost pixels: .cloud's `transform: scale(1.06)` in
 * styles.css is what keeps that fade off screen, and the two belong together.
 */
const BLUR_CSS_PX = 18

/** Idle breathing, in Hz, and how much each syllable per second adds to it. */
const IDLE_HZ = 0.08
const RATE_HZ = 0.14

/** How far the breath swells, as a fraction of the field's size. */
const BREATHE_DEPTH = 0.05

/** How much faster the blobs wander per syllable per second. */
const DRIFT_PER_RATE = 0.45

/** What loudness and a syllable's kick do to size, and what loudness does to brightness. */
const LEVEL_TO_SIZE = 0.3
const KICK_TO_SIZE = 0.18
const LEVEL_TO_GLOW = 0.28
const KICK_TO_GLOW = 0.12

/** Brightness with nothing happening. Below 1 so that speaking has somewhere to go. */
const BASE_GLOW = 0.72

/** How much of the streaks to mix in. */
const PETAL_WEIGHT = 0.55

/**
 * How much of the page's scroll the bloom takes, and the ceiling on it.
 *
 * Parallax rather than either extreme. Fixed, the light stops belonging to the
 * button once the page has moved under it; scrolling one-for-one, a long memo
 * list leaves every screen past the first flat and dark. At a third of the scroll
 * it drifts up as you read and then stops, which keeps some colour at the bottom
 * of a long list.
 */
const PARALLAX = 0.35
const PARALLAX_MAX_PX = 260

/**
 * The largest frame delta the simulation will accept, in seconds.
 *
 * requestAnimationFrame does not run in a background tab, so the first frame
 * after coming back carries the whole time away -- minutes of it. Unclamped, the
 * phases would advance by that much in one step and the field would teleport,
 * which is the one motion artefact a viewer is guaranteed to notice. Clamped, it
 * simply carries on from where it was.
 */
const MAX_DT = 0.05

const canvasEl = useTemplateRef('canvasEl')
const scrimEl = useTemplateRef('scrimEl')

const { sample } = useVoiceEnergy()

/** @type {?CanvasRenderingContext2D} */
let ctx = null

/** The offscreen the field is composed on, and its context. */
let off = null
let offCtx = null

let width = 0
let height = 0

/** The smaller viewport dimension, in backing-store pixels. Every radius is a fraction of it. */
let extent = 0

/** The anchor in viewport fractions, re-measured rather than read every frame. */
let anchor = { ...ANCHOR_FALLBACK }

/*
 * Phase, not time. Every oscillation below is integrated -- `phase += 2*pi*f*dt`
 * -- so that f can change on any frame without the value jumping. Written as
 * sin(t * f) it could not: the two frequencies either side of a change disagree
 * about where in the cycle time t is.
 */
let breathe = 0
const px = BLOBS.map(() => Math.random() * 2 * Math.PI)
const py = BLOBS.map(() => Math.random() * 2 * Math.PI)
const pr = PETALS.map((petal) => petal.phase)

let raf = 0
let previous = 0

/** @type {?ResizeObserver} */
let observer = null

const darkQuery = window.matchMedia('(prefers-color-scheme: dark)')

/*
 * Reduced motion is honoured by holding the field still, not by removing it.
 *
 * The setting is about movement rather than about colour, and a page that answers
 * it by dropping its background entirely has changed the design rather than
 * calmed it. So one frame is drawn and the loop stops -- and it stays responsive
 * to nothing, including a recording, which is the point. styles.css takes the
 * same view of the recording dot for the same reason.
 */
const stillQuery = window.matchMedia('(prefers-reduced-motion: reduce)')

/**
 * A blob's gradient: stops sampled off a gaussian centred on `ring`.
 *
 * Two stops read as a disc with a soft edge; this reads as light. The `1 - t*t`
 * factor forces the outermost stop to zero, because a gaussian never quite
 * reaches it and the residue is visible as a hard circle at the blob's rim.
 */
function gradientFor(gradient, rgb, alpha, ring, spread) {
  const head = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},`

  for (let i = 0; i < STOPS; i++) {
    const t = i / (STOPS - 1)
    const d = (t - ring) / spread

    gradient.addColorStop(t, head + (alpha * Math.exp(-d * d) * (1 - t * t)).toFixed(4) + ')')
  }

  return gradient
}

function resize() {
  const vw = window.innerWidth
  const vh = window.innerHeight

  // No devicePixelRatio, deliberately -- see RESOLUTION. Multiplying by it here
  // would undo the entire reason this is cheap.
  width = Math.max(64, Math.round(vw * RESOLUTION))
  height = Math.max(64, Math.round(vh * RESOLUTION))
  extent = Math.min(width, height)

  if (canvasEl.value) {
    canvasEl.value.width = width
    canvasEl.value.height = height
  }

  if (off) {
    off.width = width
    off.height = height
  }
}

/**
 * Find the Record button and remember where it is.
 *
 * Measured on mount, on resize, and whenever the layout changes under the
 * observer below -- the recorder row grows a timer and a second button while
 * recording, and an error banner can appear above it. Not measured per frame:
 * getBoundingClientRect forces layout, and sixty forced layouts a second to
 * follow something that moves twice a minute is the wrong trade.
 */
function measureAnchor() {
  const el = document.getElementById(ANCHOR_ID)

  if (!el) {
    anchor = { ...ANCHOR_FALLBACK }
  } else {
    const box = el.getBoundingClientRect()

    anchor = {
      x: (box.left + box.width / 2) / window.innerWidth,
      y: (box.top + window.scrollY + box.height / 2) / window.innerHeight,
    }
  }

  // The scrim is placed once, here, rather than followed per frame. It is broad
  // and weak, so the parallax offset the canvas takes is not visible in it -- and
  // what it protects is the header and the hint, which have scrolled off by the
  // time the offset is large.
  scrimEl.value?.style.setProperty('--cloud-x', (anchor.x * 100).toFixed(2) + '%')
  scrimEl.value?.style.setProperty('--cloud-y', (anchor.y * 100).toFixed(2) + '%')
}

function advance(dt, rate) {
  const driftMultiplier = 1 + rate * DRIFT_PER_RATE

  breathe += 2 * Math.PI * (IDLE_HZ + rate * RATE_HZ) * dt

  for (let i = 0; i < px.length; i++) {
    px[i] += 2 * Math.PI * BLOBS[i].fx * driftMultiplier * dt
    py[i] += 2 * Math.PI * BLOBS[i].fy * driftMultiplier * dt
  }

  for (let i = 0; i < pr.length; i++) {
    pr[i] += 2 * Math.PI * PETALS[i].fr * driftMultiplier * dt
  }
}

function draw(reading) {
  if (!ctx || !offCtx) {
    return
  }

  const light = !darkQuery.matches
  const ink = light ? LIGHT_INK : DARK_INK
  const spread = light ? LIGHT_SPREAD : 1
  const widen = light ? LIGHT_WIDEN : 0

  offCtx.setTransform(1, 0, 0, 1, 0, 0)
  offCtx.clearRect(0, 0, width, height)
  offCtx.globalCompositeOperation = light ? 'source-over' : 'lighter'

  const shift = Math.min(window.scrollY * PARALLAX, PARALLAX_MAX_PX) * RESOLUTION
  const cx = width * anchor.x
  const cy = height * anchor.y - shift

  const swell =
    1 +
    BREATHE_DEPTH * Math.sin(breathe) +
    LEVEL_TO_SIZE * reading.level +
    KICK_TO_SIZE * reading.kick
  const glow = BASE_GLOW + LEVEL_TO_GLOW * reading.level + KICK_TO_GLOW * reading.kick

  for (let i = 0; i < PETALS.length; i++) {
    const petal = PETALS[i]
    const r = petal.r * extent * (1 + (swell - 1) * 0.8)

    offCtx.save()
    offCtx.translate(cx, cy)
    offCtx.rotate(pr[i])
    offCtx.scale(1, petal.squash)
    offCtx.fillStyle = gradientFor(
      offCtx.createRadialGradient(0, 0, 0, 0, 0, r),
      petal.rgb,
      Math.min(1, petal.a * glow * PETAL_WEIGHT * (light ? 0.6 : 1)),
      petal.ring,
      petal.w,
    )
    offCtx.beginPath()
    offCtx.arc(0, 0, r, 0, 2 * Math.PI)
    offCtx.fill()
    offCtx.restore()
  }

  for (let i = 0; i < BLOBS.length; i++) {
    const blob = BLOBS[i]
    const r = blob.r * spread * extent * (1 + (swell - 1) * blob.resp)
    const x = cx + Math.sin(px[i]) * blob.ax * extent
    const y = cy + Math.sin(py[i]) * blob.ay * extent

    offCtx.fillStyle = gradientFor(
      offCtx.createRadialGradient(x, y, 0, x, y, r),
      ink[i].rgb,
      Math.min(1, ink[i].a * glow),
      blob.ring,
      blob.w + widen,
    )
    offCtx.beginPath()
    offCtx.arc(x, y, r, 0, 2 * Math.PI)
    offCtx.fill()
  }

  // Reset before clearing: a filter set on the context applies to clearRect too on
  // some engines, and a blurred clear leaves a rim of the previous frame behind.
  ctx.filter = 'none'
  ctx.clearRect(0, 0, width, height)
  ctx.filter = `blur(${(BLUR_CSS_PX * RESOLUTION).toFixed(2)}px)`
  ctx.drawImage(off, 0, 0)
}

function frame(now) {
  const t = now / 1000
  const dt = Math.min(MAX_DT, Math.max(0, t - previous))

  previous = t

  const reading = sample(dt)

  advance(dt, reading.rate)
  draw(reading)

  raf = requestAnimationFrame(frame)
}

/**
 * Run, or draw once and stop.
 *
 * No visibilityState check anywhere in here, and that is not an oversight:
 * requestAnimationFrame is already not called in a hidden tab, so a loop that
 * only ever reschedules itself from inside a frame stops on its own and restarts
 * on its own. Gating on document.hidden as well would add a second condition that
 * has to be cleared correctly, and would freeze the effect outright in any
 * embedded view that reports itself permanently hidden.
 */
function run() {
  cancelAnimationFrame(raf)
  raf = 0

  if (stillQuery.matches) {
    draw(sample(0))

    return
  }

  previous = performance.now() / 1000
  raf = requestAnimationFrame(frame)
}

function onResize() {
  resize()
  measureAnchor()

  if (stillQuery.matches) {
    draw(sample(0))
  }
}

onMounted(() => {
  ctx = canvasEl.value?.getContext('2d') ?? null

  if (!ctx) {
    // A browser with no 2d context, or a canvas the compositor refused. Nothing
    // to do and nothing to say: the page keeps its plain background.
    return
  }

  off = document.createElement('canvas')
  offCtx = off.getContext('2d')

  resize()
  measureAnchor()

  window.addEventListener('resize', onResize)
  darkQuery.addEventListener('change', onResize)
  stillQuery.addEventListener('change', run)

  // The layout moves without the window resizing: the recorder row gains a timer
  // and a Discard button while recording, and either error banner can push it
  // down. Observing the body catches all of it in one place.
  observer = new ResizeObserver(measureAnchor)
  observer.observe(document.body)

  run()
})

onUnmounted(() => {
  cancelAnimationFrame(raf)
  window.removeEventListener('resize', onResize)
  darkQuery.removeEventListener('change', onResize)
  stillQuery.removeEventListener('change', run)
  observer?.disconnect()
  observer = null
  ctx = null
  offCtx = null
  off = null
})
</script>

<template>
  <!--
    aria-hidden on all three, and no text anywhere in them: this is decoration,
    and it should not exist as far as a screen reader is concerned. They are
    siblings rather than one element with pseudo-elements so that the canvas can
    be blurred by CSS without the grain and the scrim being blurred with it.
  -->
  <canvas ref="canvasEl" class="cloud" aria-hidden="true"></canvas>
  <div class="cloud__grain" aria-hidden="true"></div>
  <div ref="scrimEl" class="cloud__scrim" aria-hidden="true"></div>
</template>
