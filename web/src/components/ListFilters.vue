<script setup>
import { computed } from 'vue'
import { CUSTOM_PRESET, DATE_PRESETS } from '../composables/useDateRange'

/*
 * A search box and a date filter, as one control.
 *
 * One component rather than two, because the brief asks for the same search and the same
 * date filter over fast memos and over collections -- and "the same" is a claim that only
 * stays true if there is one implementation of it. Three instances of this exist: the fast
 * strip, the collections grid, and an opened collection's memos. They differ in their
 * placeholder and in what they are wired to, and in nothing else.
 *
 * It owns no state. The query and the date range live in the composable driving whichever
 * list this is filtering, because that composable is what has to send them and what has to
 * decide what a stale response means. This is the control surface for them.
 */

const props = defineProps({
  /** The current search text. Bound one-way; see the input's @input for why not v-model. */
  query: { type: String, required: true },

  /**
   * The useDateRange() object driving this list.
   *
   * Passed whole rather than as four separate props plus four events, which is unusual enough
   * to justify: it is a cohesive value -- a preset and two dates that are only meaningful
   * together -- and splitting it would put the invariant that the preset and the custom dates
   * agree in this component's hands rather than in the composable's, where `set()` enforces
   * it. Nothing here writes to it directly; changes go out through `apply`.
   */
  dateRange: { type: Object, required: true },

  placeholder: { type: String, required: true },

  /** For the accessible name, which has to say *which* list this filters. */
  label: { type: String, required: true },
})

const emit = defineEmits(['search', 'search-now', 'clear', 'apply'])

/**
 * Whether the custom range's two date inputs are shown.
 *
 * Derived from the preset rather than kept as its own flag, so the panel cannot be open while
 * a preset is active -- which would be a control showing two empty dates next to a
 * highlighted "Yesterday" and no way to tell which one the list is under.
 */
const customOpen = computed(() => props.dateRange.preset === CUSTOM_PRESET)

/**
 * Today, as `YYYY-MM-DD`, for the `max` on both date inputs.
 *
 * A memo cannot have been created in the future, so a range that starts tomorrow is a filter
 * guaranteed to return nothing. The browser's own date picker greying those out is a better
 * answer than an empty list.
 *
 * From the local date rather than from toISOString(), which would give the UTC day -- and
 * would be a day off for anyone far enough east late in the evening, refusing a date that is,
 * for them, today.
 *
 * A function called from the template, not a ref set once at setup. This component is mounted
 * for as long as the page is open, so a value captured at setup is wrong for anyone who leaves
 * the tab open overnight -- and wrong in the direction that blocks them, since `max` would
 * still be yesterday and the picker would refuse today's date.
 */
function today() {
  const now = new Date()

  return [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, '0'),
    String(now.getDate()).padStart(2, '0'),
  ].join('-')
}

/** Pick a preset. */
function choose(id) {
  emit('apply', id)
}

/**
 * Edit one end of a custom range.
 *
 * Both ends are sent every time rather than only the one that changed, because the
 * composable's `set()` takes the pair -- which is what stops the two from being written by
 * two different code paths and disagreeing.
 */
function editCustom(which, value) {
  emit('apply', CUSTOM_PRESET, {
    from: which === 'from' ? value : props.dateRange.customFrom,
    to: which === 'to' ? value : props.dateRange.customTo,
  })
}
</script>

<template>
  <div class="filters">
    <!--
      A <form> wrapping the input, for the reason MemoComposer gives about its own: it tells
      the browser what the field is for, which is where "press Enter to submit" comes from
      rather than a keydown handler. submit.prevent because Enter already has a meaning here
      -- filter now, without waiting out the debounce -- and a page reload is not it.
    -->
    <form class="filters__search" role="search" @submit.prevent="emit('search-now')">
      <!--
        :value with @input rather than v-model. v-model would write the ref directly and the
        debounce would have nothing to hang off; the composable's search() sets the same ref
        and schedules the request, so the box stays responsive to every keystroke while the
        network does not.

        The thing that costs is v-model's composition guard: it suppresses its own writes
        between compositionstart and compositionend, because writing to an input mid-IME can
        cancel the composition. Checked rather than assumed, by counting writes through a
        patched value setter: typing seven characters produced *zero* writes to the element.
        Vue skips the DOM patch when the bound value already equals el.value, and search()
        sets the ref to exactly what the input just reported, so there is nothing to suppress.
      -->
      <input
        :value="query"
        class="filters__input"
        type="search"
        name="q"
        :placeholder="placeholder"
        :aria-label="label"
        autocomplete="off"
        @input="emit('search', $event.target.value)"
      />

      <button v-if="query !== ''" type="button" class="filters__clear" @click="emit('clear')">
        Clear
      </button>
    </form>

    <!--
      role="group" rather than a fieldset+legend, because the visible label would duplicate
      what the buttons already say, and a legend that has to be hidden is a sign the grouping
      wants an aria-label instead.
    -->
    <div class="filters__dates" role="group" aria-label="Filter by date">
      <button
        v-for="option in DATE_PRESETS"
        :key="option.id"
        type="button"
        class="chip"
        :class="{ 'chip--on': dateRange.preset === option.id }"
        :aria-pressed="dateRange.preset === option.id"
        @click="choose(option.id)"
      >
        {{ option.label }}
      </button>

      <!--
        aria-expanded, because this button reveals the two inputs below rather than applying a
        filter on its own -- pressing it puts the list under an empty custom range, which is
        the same as no filter until a date is typed.
      -->
      <button
        type="button"
        class="chip"
        :class="{ 'chip--on': customOpen }"
        :aria-pressed="customOpen"
        :aria-expanded="customOpen"
        @click="choose(CUSTOM_PRESET)"
      >
        Custom…
      </button>
    </div>

    <!--
      Only rendered under the custom preset. The `max` on both is today; `min` on the second
      is whatever the first holds, so the browser's picker cannot produce an inverted range --
      the API refuses one with a 422, and a control that cannot express the mistake is better
      than an error explaining it.

      Both are optional. One end alone is a real filter -- "everything since the 19th" -- and
      useDateRange sends it as an open-ended window rather than waiting for the other.
    -->
    <div v-if="customOpen" class="filters__custom">
      <label class="filters__date">
        <span>From</span>
        <input
          type="date"
          :value="dateRange.customFrom"
          :max="today()"
          @input="editCustom('from', $event.target.value)"
        />
      </label>

      <label class="filters__date">
        <span>To</span>
        <!--
          Inclusive, as far as the user is concerned: picking the 23rd includes the 23rd.
          useDateRange adds the day that turns it into the API's exclusive bound, and its
          `label` subtracts it back off for display, so the +1 never reaches the screen.
        -->
        <input
          type="date"
          :value="dateRange.customTo"
          :min="dateRange.customFrom || undefined"
          :max="today()"
          @input="editCustom('to', $event.target.value)"
        />
      </label>
    </div>
  </div>
</template>
