import { ref } from 'vue'

/*
 * How far along a memo *looks*, for the wait between `queued` and `ready`.
 *
 * Every number in this file is an estimate and the design is mostly about admitting
 * that. Nothing reports real progress: the worker claims a row, runs ffmpeg, runs a
 * model and writes once at the end, so there is no fraction to fetch and no endpoint
 * that would have one. The API's `status` is the only truth available, and it has three
 * values, none of which is "62 percent".
 *
 * So the bar is a *reassurance that the wait is normal*, not a measurement, and it is
 * built so that it cannot claim otherwise:
 *
 *   * It approaches :data:`CEILING` and never reaches it, so it can never sit full while
 *     the memo is still working -- which is the single thing that makes a progress bar
 *     read as broken.
 *   * Only a status change from the server finishes it. There is no client-side timeout
 *     that decides a memo is done, for the same reason usePolling has none.
 *   * The curve is asymptotic rather than linear, so a memo that takes far longer than
 *     usual slows down and creeps rather than overshooting and being clamped.
 */

/**
 * The time constant, from measurement rather than taste.
 *
 * A short memo -- which is nearly all of them -- reaches `ready` about five seconds
 * after it is claimed on the machine this was built on, ffmpeg and the model load
 * included. At `t = TAU` the curve is at 63 percent of the ceiling, so five seconds
 * lands a little over half way, and the common case spends its whole life in the part
 * of the curve that still visibly moves.
 */
const TAU_MS = 5_000

/**
 * The value the curve approaches and never reaches.
 *
 * Not 100, and this is the whole point. Ten minutes of audio takes about two minutes to
 * transcribe, which is twenty-four times the common case, and no fixed curve fits both.
 * A bar that creeps toward ninety while something slow happens is honest; one that hits
 * a hundred and stays there for ninety seconds looks like a hung page, and the user's
 * complaint that started this was precisely not being able to tell working from failed.
 */
const CEILING = 0.9

/**
 * How often the bar is recomputed. Not the poll interval -- that is usePolling's 2s, and
 * driving the animation off it would make the bar jump in five visible steps. 250ms is
 * smooth enough to read as motion and slow enough to be free.
 */
const TICK_MS = 250

/**
 * Progress for memos that are still working, keyed by id.
 *
 * A module-level singleton like the rest of this app's state, so two components asking
 * about the same memo get the same number.
 *
 * @returns {{
 *   progressFor: (id: string) => number,
 *   forget: (keep: Set<string>) => void,
 * }}
 */
export function useProcessingProgress() {
  return shared
}

function create() {
  /** id -> the local timestamp this memo was first seen unfinished. */
  const startedAt = new Map()

  /**
   * Bumped every tick, purely so the computed properties reading progressFor() know to
   * re-evaluate. The Map is not reactive and does not need to be -- what changes every
   * 250ms is the clock, not its contents.
   */
  const clock = ref(0)

  let timer = null

  /**
   * @param {string} id
   * @returns {number} 0 to CEILING.
   */
  function progressFor(id) {
    // Read so Vue tracks this as a dependency of whatever is rendering it. Removing
    // this line does not break anything visibly -- the bar simply stops moving until
    // something else re-renders the row.
    void clock.value

    let since = startedAt.get(id)

    if (since === undefined) {
      // First sight. The clock starts now rather than from the row's `created_at`,
      // deliberately: that column is the API's wall clock and this is the browser's,
      // and usePolling already declined to compare the two. The cost is that a memo
      // already in flight when the page loads starts its bar at zero, which understates
      // the wait -- an honest direction to be wrong in, since the alternative is a bar
      // that jumps to 80 percent on a page refresh.
      since = Date.now()
      startedAt.set(id, since)
      start()
    }

    return CEILING * (1 - Math.exp(-(Date.now() - since) / TAU_MS))
  }

  /**
   * Drop the clocks for memos that are no longer waiting.
   *
   * Called by whoever knows the current list, because this file deliberately knows
   * nothing about memos or statuses. Without it the Map grows for the life of the tab --
   * small, but it is also what stops a memo that failed and was retried from resuming
   * its old bar at 90 percent instead of starting again.
   *
   * @param {Set<string>} keep Ids still unfinished.
   */
  function forget(keep) {
    for (const id of startedAt.keys()) {
      if (!keep.has(id)) {
        startedAt.delete(id)
      }
    }

    if (startedAt.size === 0) {
      stop()
    }
  }

  function start() {
    if (timer === null) {
      timer = setInterval(tick, TICK_MS)
    }
  }

  /**
   * The interval stops itself when there is nothing left to animate, rather than relying
   * on forget() being called. No lifecycle hook here to do it instead: this is a
   * module-level singleton like useMemos, so there is no component whose unmount would
   * be the right moment, and calling onUnmounted outside a setup() only produces a
   * warning.
   */
  function tick() {
    if (startedAt.size === 0) {
      stop()

      return
    }

    clock.value += 1
  }

  function stop() {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }

  return { progressFor, forget }
}

const shared = create()
