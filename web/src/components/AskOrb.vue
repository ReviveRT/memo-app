<script setup>
import { onMounted, onUnmounted, ref, useTemplateRef, watch } from 'vue'

/*
 * The ask widget's sphere: a small energy orb that breathes on its own and quickens while an
 * answer is being written.
 *
 * A canvas rather than CSS keyframes, and the argument is the one MemoBackdrop makes about the
 * background bloom: the *rate* changes -- the orb spins and pulses faster the moment a question
 * is in flight -- and a keyframe animation cannot be retimed. Reassigning animation-duration
 * restarts the interpolation, so every change of state would land as a visible jump. Here the
 * frequencies are integrated into phases (see advance) and a rate change is continuous.
 *
 * It is a *second* canvas on a page that already has one, which is worth justifying rather than
 * assuming -- and the justification got weaker when the sphere doubled to 8rem, so here are the
 * real numbers rather than the reassuring ones. The backdrop's offscreen is 317x179 at a
 * typical window, about 57 thousand pixels. This is 256x256 at devicePixelRatio 2, about 66
 * thousand: no longer the rounding error it was at half the size. What is drawn into them is
 * still small -- eight arcs and a handful of gradients -- and the cost that scales is the two
 * blurred composites at the end, which is why MAX_BACKING exists to stop the backing store
 * growing with the element for ever. BLOOM has why the blur is done on the offscreen at all
 * rather than as a CSS filter on the stretched element, which is where it would be ruinous.
 *
 * Everything here is decoration. The canvas is inside a button that works with no canvas at
 * all: a failure to get a 2d context falls back to a plain ringed circle (see `blank`), and
 * the widget is still openable. That property is worth keeping.
 */

const props = defineProps({
  /**
   * Whether an answer is being produced right now.
   *
   * Drives the rate and the brightness rather than swapping in a second animation, which is
   * the whole reason this is integrated: the orb speeds up over about a second instead of
   * cutting to a busy state, and slows back down the same way. See `energy`.
   */
  active: { type: Boolean, default: false },

  /** Whether the widget's panel is open, which the orb answers by burning a little brighter. */
  open: { type: Boolean, default: false },
})

/*
 * The orbital arcs, drawn back to front.
 *
 * Each is an ellipse -- a circle squashed on one axis and then rotated -- which is what makes
 * a flat 2d canvas read as rings going *around* something rather than as circles drawn on top
 * of it. `squash` is how far the ring is tipped away from the viewer and `tilt` is which way it
 * leans; the two together are the whole of the 3d in this, and no depth sorting is needed
 * because everything is composited additively.
 *
 * `span` is how much of the ring is lit. None of them is a full circle, deliberately: a
 * complete ellipse reads as a wire frame, and the reference is light travelling along a path.
 * The alpha ramp in `arc()` puts the bright end at the leading edge, which is what makes each
 * one read as a comet rather than as a dash.
 *
 * The speeds are deliberately not in any simple ratio to each other. Two rings at a rational
 * ratio re-synchronise every few turns and the eye finds that period; these do not repeat on
 * any timescale somebody is looking at a button for. Signs differ so the sphere does not read
 * as one rotating object.
 */
const ARCS = [
  { r: 0.92, tilt: 0.34, squash: 0.26, span: 2.6, speed: 0.21, w: 0.05, rgb: [150, 92, 255] },
  { r: 0.85, tilt: -0.87, squash: 0.44, span: 3.2, speed: -0.163, w: 0.05, rgb: [255, 74, 190] },
  { r: 0.79, tilt: 1.93, squash: 0.18, span: 2.3, speed: 0.287, w: 0.044, rgb: [96, 132, 255] },
  { r: 0.72, tilt: 2.71, squash: 0.5, span: 3.9, speed: -0.117, w: 0.046, rgb: [186, 70, 255] },
  { r: 0.64, tilt: -0.41, squash: 0.55, span: 3.7, speed: -0.239, w: 0.042, rgb: [255, 110, 220] },
  { r: 0.57, tilt: 1.24, squash: 0.32, span: 2.9, speed: 0.331, w: 0.04, rgb: [110, 160, 255] },
  { r: 0.5, tilt: -1.66, squash: 0.62, span: 4.2, speed: 0.196, w: 0.036, rgb: [64, 232, 214] },
  { r: 0.43, tilt: 0.83, squash: 0.4, span: 3.3, speed: -0.377, w: 0.034, rgb: [120, 255, 236] },
]

/**
 * How many strokes each arc is drawn as.
 *
 * The alpha has to vary along the arc and a canvas stroke has one colour, so an arc is a run
 * of short segments rather than one path -- which is also what makes `lineCap` load-bearing
 * rather than a detail. See `arc()`.
 */
const SEGMENTS = 26

/** The teal core, and the specks drifting inside it. */
const CORE_RGB = [56, 240, 214]
const CORE_R = 0.4

/**
 * The specks: tiny bright points on their own orbits, standing in for the plasma texture in
 * the reference. At this size the texture itself is invisible, and what survives is that the
 * middle of the orb is not a plain disc.
 */
const SPECKS = [
  { r: 0.2, squash: 0.5, tilt: 0.6, speed: 0.51, size: 0.075, rgb: [190, 255, 245] },
  { r: 0.27, squash: 0.35, tilt: -1.2, speed: -0.43, size: 0.06, rgb: [120, 255, 226] },
  { r: 0.14, squash: 0.7, tilt: 2.4, speed: 0.62, size: 0.05, rgb: [255, 255, 255] },
]

/** The violet halo the whole thing sits in. */
const HALO_RGB = [128, 80, 240]

/**
 * The dark base the energy is drawn on, and how far it reaches.
 *
 * **This is what makes the orb work on a light page, and it is not a light-scheme special
 * case.** Additive light on white is white: without something dark underneath, every arc and
 * the core all wash out and the button becomes a pale smudge. The reference image has the same
 * thing -- the sphere is on deep navy, and that navy is part of why the cyan reads as hot.
 *
 * So it is drawn in both schemes, and only its strength differs. On the dark page it is a
 * shade deeper than the background and reads as depth; on the light one it is the object's own
 * body. Soft-edged rather than a disc, because a hard rim would make it a coloured circle with
 * a picture in it rather than something glowing.
 */
const BASE_RGB = [10, 14, 34]
const BASE_ALPHA = { dark: 0.72, light: 0.88 }
const BASE_R = 0.98

/**
 * The two composites the glow is made of.
 *
 * One heavy blur for the bloom and one just under a pixel to take the stair-stepping off the
 * arcs, both applied to the small offscreen as it is copied across rather than as a CSS
 * `filter` on the element. That is the same choice MemoBackdrop's BLUR_CSS_PX records, and the
 * reason there was measured: blurring the displayed element re-rasterises every displayed pixel
 * every frame, and it cost more than everything else on that page put together. Here it is two
 * passes over roughly sixteen thousand pixels.
 *
 * Bloom first and the crisp copy second, so the sharp arcs sit on top of their own light
 * rather than under it.
 *
 * **BLOOM is a fraction of the orb rather than a pixel count, and that is the correction to a
 * version that used pixels.** The bloom is what fuses eight thin trails into one ball of
 * light, so its size relative to the sphere *is* the picture -- fixed at 7px it was a tenth of
 * the width at the size the widget actually draws it and a thirtieth when scaled up to look at,
 * which made the same code read as a glowing sphere in one place and as loose wires in the
 * other. SHARP stays in device pixels, because what it smooths is the rasteriser's stair-
 * stepping and that is a property of pixels rather than of the drawing.
 *
 * `ctx.filter` is unsupported on Safari before 17, where both assignments are ignored. The
 * result there is an unbloomed orb -- thin bright arcs, no halo -- which is duller than
 * intended and not broken.
 */
const BLOOM = 0.115
const SHARP_PX = 0.7

/** Idle rotation and breathing, in Hz, and what a question in flight adds to each. */
const IDLE_SPIN = 1
const ACTIVE_SPIN = 2.4
const IDLE_PULSE_HZ = 0.34
const ACTIVE_PULSE_HZ = 0.85

/** How far the breath swells the sphere, and how much it brightens it. */
const PULSE_SIZE = 0.055
const PULSE_GLOW = 0.16

/** Brightness at rest, and what being open or busy adds. */
const BASE_GLOW = 0.95
const OPEN_GLOW = 0.12
const ACTIVE_GLOW = 0.3

/**
 * How fast the orb takes up or lets go of the busy state, per second.
 *
 * `energy` chases 0 or 1 at this rate rather than jumping, so pressing Ask spins the sphere up
 * over about half a second instead of cutting to a second animation -- which is the thing a
 * keyframe version could not do at all. Exponential rather than a fixed duration for the reason
 * MemoBackdrop's ANCHOR_EASE gives: the target can change again mid-flight, and this simply
 * retargets where an interpolation would have to restart and would visibly stutter.
 */
const ENERGY_EASE = 3.2

/**
 * The largest the backing store is allowed to get, in device pixels.
 *
 * **A ceiling rather than a resolution, and it exists because the sphere doubled.** Rendering
 * at devicePixelRatio and no more was fine while the orb was 4rem: 128 device pixels square,
 * a quarter of what the backdrop composes. At 8rem on a retina screen that is 256 square and
 * four times the area, and the part that scales with it is the two blurred composites -- the
 * bloom is a *fraction* of the canvas, so a bigger canvas means both more pixels and a wider
 * blur kernel over each of them.
 *
 * 192 is where that stops growing. It costs a 1.33x upscale by the compositor at 8rem, for
 * nothing, on content that is entirely soft gradients and deliberately blurred strokes -- the
 * same trade MemoBackdrop's RESOLUTION makes far more aggressively for the same reason. What
 * it buys is that the next person to want a bigger sphere changes one number in the stylesheet
 * and pays for it in layout rather than in frame time.
 */
const MAX_BACKING = 192

/**
 * The largest frame delta the simulation will accept, in seconds.
 *
 * requestAnimationFrame does not run in a background tab, so the first frame after coming back
 * carries the whole time away. Unclamped, the phases would advance by minutes in one step and
 * the arcs would teleport.
 */
const MAX_DT = 0.05

const canvasEl = useTemplateRef('canvasEl')

/**
 * Whether the sphere could not be drawn at all, and the button needs to look like something.
 *
 * **Without this the graceful degradation at the top of this file is not true.** The button
 * around the canvas has no border and no background -- the drawing *is* its appearance -- so a
 * browser that refuses a 2d context leaves a 4rem square of nothing in the corner, keyboard
 * reachable and named for a screen reader and completely invisible to everybody else. That is
 * the whole feature gone, for a failure that should cost only the animation.
 *
 * A ref rather than reaching for the element's classList, so the fallback is part of what this
 * component renders rather than something it does to the DOM behind Vue's back.
 */
const blank = ref(false)

/** @type {?CanvasRenderingContext2D} */
let ctx = null

/** The offscreen the energy is composed on before it is bloomed onto the canvas. */
let off = null
let offCtx = null

/** The backing store's size in device pixels, and the scale from displayed pixels to them. */
let size = 0
let scale = 1

/*
 * Phase, not time -- integrated so a frequency can change on any frame without the value
 * jumping. Started at each arc's own offset rather than at zero so the sphere does not begin
 * with every ring in step, which is the one moment somebody definitely sees.
 */
const spin = ARCS.map((_, i) => i * 1.31)
const specks = SPECKS.map((_, i) => i * 2.11)
let pulse = 0

/** How busy the orb currently reads as, 0 to 1. Chases `props.active`. See ENERGY_EASE. */
let energy = 0

let raf = 0
let previous = 0

/** @type {?ResizeObserver} */
let observer = null

const darkQuery = window.matchMedia('(prefers-color-scheme: dark)')

/*
 * Reduced motion holds the sphere still rather than removing it, which is the view
 * MemoBackdrop and the answer caret both take: the setting is about movement, and a button
 * that answers it by going blank has changed what it is rather than calming it. One frame is
 * drawn and the loop stops -- including while an answer streams, which is the point.
 */
const stillQuery = window.matchMedia('(prefers-reduced-motion: reduce)')

/** A point on a tipped, leaning ring at angle `a`. See ARCS for what the two numbers mean. */
function ringPoint(a, r, tilt, squash) {
  const c = Math.cos(a)
  const s = Math.sin(a) * squash

  return [
    r * (Math.cos(tilt) * c - Math.sin(tilt) * s),
    r * (Math.sin(tilt) * c + Math.cos(tilt) * s),
  ]
}

/** One arc: short strokes along a ring, brightest at the leading end. */
function arc(spec, phase, radius, glow) {
  const head = `rgba(${spec.rgb[0]},${spec.rgb[1]},${spec.rgb[2]},`

  offCtx.lineWidth = Math.max(1, spec.w * radius)

  /*
   * **butt, and it is the whole reason a trail reads as a trail.** With round caps every
   * segment is drawn half a line-width past each end, so consecutive segments overlap by a
   * full cap -- and under `lighter` an overlap is not a redraw but an addition, so each of the
   * twenty-six joins comes out brighter than the line around it. The result is a string of
   * beads, evenly spaced, that looks like a deliberate dotted texture rather than like a bug.
   *
   * Butt caps leave a hairline notch at each join instead, of about half a pixel at these
   * angles, and the bloom pass closes it.
   */
  offCtx.lineCap = 'butt'

  for (let i = 0; i < SEGMENTS; i++) {
    // Squared rather than linear, so the trail fades away over most of its length and the
    // light is concentrated in the last few segments. Linear reads as a painted stripe.
    const t = i / SEGMENTS
    const from = phase + t * spec.span
    const to = phase + ((i + 1) / SEGMENTS) * spec.span

    const [x1, y1] = ringPoint(from, spec.r * radius, spec.tilt, spec.squash)
    const [x2, y2] = ringPoint(to, spec.r * radius, spec.tilt, spec.squash)

    offCtx.strokeStyle = head + (t * t * glow).toFixed(3) + ')'
    offCtx.beginPath()
    offCtx.moveTo(x1, y1)
    offCtx.lineTo(x2, y2)
    offCtx.stroke()
  }

  // The hot head. Without it every arc simply stops, and a trail with no source reads as a
  // gap in a ring rather than as something travelling along one.
  const [hx, hy] = ringPoint(phase + spec.span, spec.r * radius, spec.tilt, spec.squash)
  const dot = spec.w * radius * 1.6

  offCtx.fillStyle = blob(hx, hy, dot, spec.rgb, Math.min(1, glow * 1.1))
  offCtx.beginPath()
  offCtx.arc(hx, hy, dot, 0, 2 * Math.PI)
  offCtx.fill()
}

/** A soft round gradient, which is nearly everything that is not an arc. */
function blob(x, y, r, rgb, alpha, ramp = 1) {
  const gradient = offCtx.createRadialGradient(x, y, 0, x, y, r)
  const head = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},`

  // Five stops on a curve rather than two, for the reason MemoBackdrop's gradientFor gives at
  // greater length: two stops read as a disc with a soft edge, and a curve reads as light.
  for (let i = 0; i <= 4; i++) {
    const t = i / 4

    gradient.addColorStop(t, head + (alpha * Math.pow(1 - t, ramp * 2.2)).toFixed(4) + ')')
  }

  return gradient
}

function resize() {
  const el = canvasEl.value

  if (!el || !ctx) {
    return
  }

  const box = el.getBoundingClientRect()
  const css = Math.max(24, Math.round(Math.min(box.width, box.height)))

  // Capped at 2, which is where the returns stop on content that is nothing but gradients and
  // blurred strokes -- and the cap is what keeps a 3x phone from quadrupling this for nothing.
  // Then capped again in absolute terms, which is MAX_BACKING's argument.
  size = Math.min(MAX_BACKING, Math.round(css * Math.min(2, window.devicePixelRatio || 1)))

  // Derived from the size that survived both caps rather than from devicePixelRatio, because
  // it is what one displayed pixel is actually worth here -- and SHARP_PX, the only thing that
  // reads it, is smoothing the rasteriser's stair-stepping and has to be in those units.
  scale = size / css

  el.width = size
  el.height = size

  if (off) {
    off.width = size
    off.height = size
  }
}

function advance(dt) {
  // The chase, integrated like the phases: `1 - exp(-k*dt)` rather than a step per frame, so
  // the spin-up takes the same half second at 60 Hz and at 120.
  energy += ((props.active ? 1 : 0) - energy) * (1 - Math.exp(-ENERGY_EASE * dt))

  const rate = IDLE_SPIN + (ACTIVE_SPIN - IDLE_SPIN) * energy

  for (let i = 0; i < spin.length; i++) {
    spin[i] += 2 * Math.PI * ARCS[i].speed * rate * dt
  }

  for (let i = 0; i < specks.length; i++) {
    specks[i] += 2 * Math.PI * SPECKS[i].speed * rate * dt
  }

  pulse += 2 * Math.PI * (IDLE_PULSE_HZ + (ACTIVE_PULSE_HZ - IDLE_PULSE_HZ) * energy) * dt
}

function draw() {
  if (!ctx || !offCtx || size === 0) {
    return
  }

  const light = !darkQuery.matches
  const mid = size / 2

  // Every radius below is a fraction of this, so the whole picture scales with the element and
  // nothing has to be re-tuned when the widget is smaller on a phone. Short of the half-width,
  // because the bloom spreads and a sphere drawn to the edge would have its halo clipped.
  const radius = mid * 0.74
  const swell = 1 + PULSE_SIZE * Math.sin(pulse)
  const glow =
    BASE_GLOW +
    PULSE_GLOW * Math.sin(pulse) +
    (props.open ? OPEN_GLOW : 0) +
    ACTIVE_GLOW * energy

  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.filter = 'none'
  ctx.clearRect(0, 0, size, size)

  // The dark body first, straight onto the canvas rather than through the bloom. Composited
  // with the energy it would be added to rather than sat under, which is the opposite of what
  // it is for. See BASE_RGB.
  ctx.globalCompositeOperation = 'source-over'

  const base = ctx.createRadialGradient(mid, mid, 0, mid, mid, mid * BASE_R)
  const baseHead = `rgba(${BASE_RGB[0]},${BASE_RGB[1]},${BASE_RGB[2]},`
  const baseAlpha = light ? BASE_ALPHA.light : BASE_ALPHA.dark

  for (let i = 0; i <= 5; i++) {
    const t = i / 5

    base.addColorStop(t, baseHead + (baseAlpha * Math.pow(1 - t, 1.7)).toFixed(4) + ')')
  }

  ctx.fillStyle = base
  ctx.beginPath()
  ctx.arc(mid, mid, mid * BASE_R, 0, 2 * Math.PI)
  ctx.fill()

  offCtx.setTransform(1, 0, 0, 1, 0, 0)
  offCtx.clearRect(0, 0, size, size)
  offCtx.globalCompositeOperation = 'lighter'
  offCtx.translate(mid, mid)

  const r = radius * swell

  // The halo, under everything. Wide and weak: it is what fills the space between the arcs so
  // they read as belonging to one object.
  offCtx.fillStyle = blob(0, 0, r * 1.15, HALO_RGB, 0.44 * glow, 1.5)
  offCtx.beginPath()
  offCtx.arc(0, 0, r * 1.15, 0, 2 * Math.PI)
  offCtx.fill()

  for (let i = 0; i < ARCS.length; i++) {
    arc(ARCS[i], spin[i], r, Math.min(1, glow * 0.95))
  }

  // The core, over the arcs rather than under them: the inner rings pass in front of it in the
  // reference and behind it here, and at this size the difference is invisible while the
  // brightness of a core that is not crossed by anything is not.
  const core = r * CORE_R

  offCtx.fillStyle = blob(0, 0, core, CORE_RGB, Math.min(1, 0.95 * glow), 1.15)
  offCtx.beginPath()
  offCtx.arc(0, 0, core, 0, 2 * Math.PI)
  offCtx.fill()

  for (let i = 0; i < SPECKS.length; i++) {
    const speck = SPECKS[i]
    const [x, y] = ringPoint(specks[i], speck.r * r, speck.tilt, speck.squash)
    const dot = speck.size * r

    offCtx.fillStyle = blob(x, y, dot, speck.rgb, Math.min(1, 0.9 * glow))
    offCtx.beginPath()
    offCtx.arc(x, y, dot, 0, 2 * Math.PI)
    offCtx.fill()
  }

  // Bloom, then the crisp copy on top of its own light. See BLOOM.
  ctx.globalCompositeOperation = 'lighter'
  ctx.filter = `blur(${(BLOOM * size).toFixed(2)}px)`
  ctx.drawImage(off, 0, 0)
  ctx.filter = `blur(${(SHARP_PX * scale).toFixed(2)}px)`
  ctx.drawImage(off, 0, 0)
  ctx.filter = 'none'
}

function frame(now) {
  const t = now / 1000
  const dt = Math.min(MAX_DT, Math.max(0, t - previous))

  previous = t

  advance(dt)
  draw()

  raf = requestAnimationFrame(frame)
}

/**
 * Run, or draw once and stop.
 *
 * No visibilityState check, deliberately: requestAnimationFrame is already not called in a
 * hidden tab, so a loop that only reschedules itself from inside a frame stops and restarts on
 * its own. Gating on document.hidden as well would freeze the orb outright in any embedded
 * view that reports itself permanently hidden -- which is a real browser, not a hypothetical.
 */
function run() {
  cancelAnimationFrame(raf)
  raf = 0

  if (stillQuery.matches) {
    // Snapped rather than eased, so a still orb is drawn in the state it is actually in
    // instead of the state it was easing out of.
    energy = props.active ? 1 : 0

    draw()

    return
  }

  previous = performance.now() / 1000
  raf = requestAnimationFrame(frame)
}

function onResize() {
  resize()

  if (stillQuery.matches) {
    draw()
  }
}

/*
 * Redrawn on a state change while motion is reduced, and nothing else needs it: the running
 * loop reads both props every frame. Without this the still orb would never pick up the
 * brightness that says a question is in flight.
 */
watch(
  () => [props.active, props.open],
  () => {
    if (stillQuery.matches) {
      run()
    }
  },
)

onMounted(() => {
  ctx = canvasEl.value?.getContext('2d') ?? null
  off = document.createElement('canvas')
  offCtx = off?.getContext('2d') ?? null

  // Both contexts, because the offscreen is where every arc and gradient is composed -- one
  // without the other draws nothing and would leave `blank` false, which is the invisible
  // button this check exists to prevent.
  if (!ctx || !offCtx) {
    blank.value = true
    ctx = null
    offCtx = null
    off = null

    return
  }

  resize()

  darkQuery.addEventListener('change', onResize)
  stillQuery.addEventListener('change', run)

  // The element's own size rather than the window's: the widget is smaller below 40rem, and
  // that change arrives as a layout change rather than always as a resize.
  observer = new ResizeObserver(onResize)
  observer.observe(canvasEl.value)

  run()
})

onUnmounted(() => {
  cancelAnimationFrame(raf)
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
    aria-hidden, and no text in it: this is decoration inside a button that carries the label.
    A screen reader announcing "canvas" beside "Ask your memos" would be describing the paint.
  -->
  <canvas
    ref="canvasEl"
    class="orb"
    :class="{ 'orb--blank': blank }"
    aria-hidden="true"
  ></canvas>
</template>
