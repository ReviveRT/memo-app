<script setup>
/*
 * One bar, two honesties.
 *
 * `value` is a fraction from 0 to 1 when something real is being measured, and null
 * when nothing is -- and the difference is not cosmetic. A determinate bar carries
 * `aria-valuenow`, so a screen reader says "40 percent"; an indeterminate one omits it
 * and says "busy". Filling a bar from a number nobody measured would make that
 * announcement a lie, which is why null is a supported value rather than defaulting to
 * zero.
 *
 * Presentational only. Where the number comes from, and whether it deserves to be
 * believed, is the caller's problem -- see useProcessingProgress for the one case here
 * where it is an estimate and says so.
 */
import { computed } from 'vue'

const props = defineProps({
  /**
   * 0 to 1, or null for "working, no idea how far". Clamped rather than trusted: a
   * fraction over 1 would render as a bar wider than its track, which reads as a
   * layout bug rather than as bad input.
   */
  value: { type: Number, default: null },

  /**
   * What is progressing, for anyone who cannot see the bar. Required, because a
   * progressbar with no accessible name is announced as "progress bar" and nothing
   * else -- and this component is used twice on the same page.
   */
  label: { type: String, required: true },
})

const fraction = computed(() =>
  props.value === null ? null : Math.min(1, Math.max(0, props.value)),
)

const percent = computed(() => (fraction.value === null ? null : Math.round(fraction.value * 100)))
</script>

<template>
  <!--
    aria-valuenow is bound to null on the indeterminate path, which removes the
    attribute rather than setting it to "null" -- that is what tells assistive
    technology the value is unknown. aria-valuemin and max stay: they are what makes
    the announced value a percentage rather than a bare number.
  -->
  <div
    class="progress"
    role="progressbar"
    :aria-label="label"
    aria-valuemin="0"
    aria-valuemax="100"
    :aria-valuenow="percent"
    :class="{ 'progress--indeterminate': fraction === null }"
  >
    <div
      class="progress__fill"
      :style="fraction === null ? null : { width: `${fraction * 100}%` }"
    ></div>
  </div>
</template>
