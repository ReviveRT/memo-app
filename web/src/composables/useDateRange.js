import { computed, reactive, ref } from 'vue'

/*
 * The date filter, as a pair of instants the API can compare against `created_at`.
 *
 * One factory, used by every list that filters by date -- the fast strip, an opened
 * collection, and the collections grid. The brief asks for the same filter in all of them,
 * and this is where "the same" is defined: the same presets, the same custom range, and the
 * same conversion from a calendar day to a pair of instants.
 *
 * **The two hard parts are both about timezones, and both are the browser's job.**
 *
 * 1. "Yesterday" is a local question. The same instant is Sunday in Auckland and Saturday in
 *    Los Angeles, so only the browser can say which 24 hours somebody meant. It converts to
 *    absolute instants before sending, which is why the API has no `tz` parameter and no
 *    timezone opinion to get wrong. App\Support\TimeWindow states the same division of
 *    labour from the other side.
 *
 * 2. The interval is half-open: `from` inclusive, `to` exclusive. So a single day runs from
 *    its own midnight to the *next* day's midnight, and a range of 19-23 July ends at
 *    midnight on the 24th. The tempting alternative -- ending at 23:59:59 -- silently drops
 *    every memo written in the last second of the range, and at the millisecond precision
 *    the API actually stores, the last 999 milliseconds too. Nothing surfaces; the list is
 *    just short, at the boundary, for the newest rows.
 *
 * The API refuses `to <= from` with a 422 rather than answering an empty list, which is the
 * backstop for getting the +1 day wrong here: the bug would otherwise show up as "the filter
 * returns nothing" rather than as an error naming the range.
 */

/**
 * The presets, in the order they are offered.
 *
 * "Yesterday" is on the list because the brief names it, and it is the one preset that is
 * awkward to express with two date pickers -- it needs yesterday's date in one and today's
 * in the other, which reads like an off-by-one even when it is right.
 *
 * @type {Array<{id: string, label: string, days: ?{from: number, to: number}}>}
 */
export const DATE_PRESETS = [
  // `days` is an offset in whole local days from today: `from: 0, to: 1` is "midnight this
  // morning until midnight tonight". Expressed as offsets rather than as computed dates so
  // the table stays a table -- nothing here calls new Date().
  { id: 'all', label: 'Any time', days: null },
  { id: 'today', label: 'Today', days: { from: 0, to: 1 } },
  { id: 'yesterday', label: 'Yesterday', days: { from: -1, to: 0 } },
  { id: 'week', label: 'Last 7 days', days: { from: -6, to: 1 } },
]

/** The id used when the two date inputs are driving instead of a preset. */
export const CUSTOM_PRESET = 'custom'

/**
 * Midnight at the start of a local day, `offset` days from today, as an ISO instant.
 *
 * `new Date(y, m, d)` rather than anything string-based, because the local-midnight part is
 * the whole point. This is also why the custom range below is not simply passed through:
 * `new Date('2026-07-19')` parses an ISO *date* as UTC midnight, so for anyone west of
 * Greenwich it is the evening of the 18th -- a filter for "the 19th" that quietly starts
 * some hours early and ends some hours early. Feeding the components to the constructor
 * instead gets local midnight in every zone.
 *
 * Day arithmetic through the day-of-month argument, which is allowed to overflow: `new
 * Date(2026, 6, 0)` is 30 June, and `new Date(2026, 6, 32)` is 1 August. That is what makes
 * this correct across month and year boundaries, and across a daylight-saving change --
 * the constructor resolves the calendar date first and then applies the offset in force on
 * *that* day, which is what "midnight local time" has to mean.
 */
function localMidnight(year, monthIndex, day, offset = 0) {
  return new Date(year, monthIndex, day + offset).toISOString()
}

/** Today's calendar components, in the reader's own timezone. */
function today() {
  const now = new Date()

  return [now.getFullYear(), now.getMonth(), now.getDate()]
}

/**
 * Splits a `YYYY-MM-DD` value from an `<input type="date">` into calendar components.
 *
 * Returns null for anything that is not a complete, plausible date, which covers the two
 * states an empty or half-typed date input reports: '' and a value the browser has not
 * finished validating. Callers treat null as "this end is not set yet" rather than as an
 * error, because a user filling in the second box has had exactly one end set for as long
 * as it took them to type.
 */
function parseDateInput(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value ?? '')

  if (match === null) {
    return null
  }

  return [Number(match[1]), Number(match[2]) - 1, Number(match[3])]
}

export function useDateRange() {
  /** Which preset is selected, or CUSTOM_PRESET. */
  const preset = ref('all')

  /** The two `<input type="date">` values, `YYYY-MM-DD` or ''. */
  const customFrom = ref('')
  const customTo = ref('')

  /**
   * The window, as the two instants the API takes. Either end may be null for "unbounded".
   *
   * A custom range with only one end filled in is a valid, useful filter -- "everything
   * since the 19th" -- so it is sent rather than withheld until both are set. That is also
   * what makes typing into the boxes feel live instead of doing nothing until the second one
   * is complete.
   */
  const window = computed(() => {
    if (preset.value !== CUSTOM_PRESET) {
      const chosen = DATE_PRESETS.find((option) => option.id === preset.value)

      if (!chosen?.days) {
        return { from: null, to: null }
      }

      const [year, month, day] = today()

      return {
        from: localMidnight(year, month, day, chosen.days.from),
        to: localMidnight(year, month, day, chosen.days.to),
      }
    }

    const start = parseDateInput(customFrom.value)
    const end = parseDateInput(customTo.value)

    return {
      from: start === null ? null : localMidnight(...start),

      // The +1 is the half-open contract, and it is the single most important line in this
      // file. The user picked the last day they want *included*; the API's bound is
      // exclusive. Without it, a range ending on the 23rd stops at midnight *starting* the
      // 23rd and silently excludes that whole day -- which reads as the filter being
      // off-by-one rather than as anything to do with interval conventions.
      to: end === null ? null : localMidnight(end[0], end[1], end[2], 1),
    }
  })

  const from = computed(() => window.value.from)
  const to = computed(() => window.value.to)

  /** Whether this filter is narrowing anything. Drives the "clear" affordance. */
  const isActive = computed(() => from.value !== null || to.value !== null)

  /**
   * A short human description of the window, for the caption under a filtered list.
   *
   * Built from the *inputs* rather than from the instants, which is why the preset name
   * comes back verbatim: "Yesterday" is what the user chose and what they will recognise,
   * where a rendered pair of timestamps is something they would have to decode.
   *
   * The custom case subtracts the day back off before displaying, so the label names the
   * last day included rather than the exclusive bound. Showing the raw `to` here would put
   * the +1 in front of the user, and it would look like a bug in exactly the way the
   * half-open interval is designed to avoid being one.
   */
  const label = computed(() => {
    if (preset.value !== CUSTOM_PRESET) {
      return DATE_PRESETS.find((option) => option.id === preset.value)?.label ?? null
    }

    const start = customFrom.value
    const end = customTo.value

    if (start !== '' && end !== '') {
      return `${start} to ${end}`
    }

    if (start !== '') {
      return `since ${start}`
    }

    if (end !== '') {
      return `up to ${end}`
    }

    return null
  })

  /**
   * Choose a preset, or a custom range.
   *
   * One setter for both, so a caller cannot leave `preset` saying 'today' while the custom
   * inputs hold something else -- a state where what the list shows and what the control
   * says would disagree.
   *
   * @param {string} next A preset id, or CUSTOM_PRESET.
   * @param {{from?: string, to?: string}} [custom] Only read for CUSTOM_PRESET.
   */
  function set(next, custom = {}) {
    preset.value = next

    if (next === CUSTOM_PRESET) {
      // Defaulted to the current values rather than to '' so that editing one box does not
      // silently clear the other.
      customFrom.value = custom.from ?? customFrom.value
      customTo.value = custom.to ?? customTo.value

      return
    }

    // Cleared when leaving custom, so returning to it starts empty rather than re-applying
    // dates the user last saw days ago.
    customFrom.value = ''
    customTo.value = ''
  }

  /** Back to unfiltered. */
  function clear() {
    set('all')
  }

  /*
   * `reactive`, not a plain object of refs, and this is the one line here that is about Vue
   * rather than about dates.
   *
   * Vue unwraps refs in a template only for *top-level* setup bindings. A plain object holding
   * refs is not unwrapped through property access, so `dateRange.label` in a template would
   * render the Ref itself -- "[object Object]" -- and `v-if="dateRange.isActive"` would be
   * testing an object, which is always truthy. Both failures are silent in the sense that
   * nothing throws; the filter would simply always describe itself as active.
   *
   * `reactive()` unwraps refs on property access, which fixes the template and has the useful
   * side effect of making the JS read the same way: `dateRange.from`, in a component and in a
   * composable alike, with no `.value` to remember on one side and not the other. That is why
   * this object is handed to ListFilters whole rather than as eight props.
   */
  return reactive({ preset, customFrom, customTo, from, to, isActive, label, set, clear })
}
