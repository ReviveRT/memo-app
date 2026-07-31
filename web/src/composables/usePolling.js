import { onUnmounted, ref, watch } from 'vue'

/*
 * The timer behind the list, and nothing about memos.
 *
 * `active` is a ref saying whether there is still something to wait for and `tick` is
 * what to do about it. Keeping the two apart is what lets the stop condition live next
 * to the statuses it is about (composables/useMemos.js) while the decay, the pause and
 * the hint live here, and it is why this file mentions no status at all.
 */

/**
 * 2s while the work is young, 5s once a minute of it has passed.
 *
 * The fast interval is almost the whole latency on today's happy path: the MEMO-09 gate
 * measured a text memo at 2-6ms of actual work, so a memo reaches `ready` about a poll
 * interval after it is saved rather than about a job later. Two seconds is short enough
 * that the transition reads as immediate and long enough that a stopped api container
 * is not being asked 20 times a second.
 *
 * The decay is for the case the fast interval is wrong about. Real transcription
 * (MEMO-14) runs 10 to 30 seconds and a long recording longer, and past the first
 * minute the 1,080 extra requests an hour buy nothing that 5s does not.
 */
export const FAST_INTERVAL_MS = 2_000
export const SLOW_INTERVAL_MS = 5_000
export const DECAY_AFTER_MS = 60_000

/**
 * When to admit that this is taking a while.
 *
 * Below the decay threshold on purpose: the hint should arrive while the poll is still
 * fast, so the reassurance and the transition it is apologising for cannot cross.
 *
 * It changes what is on screen and nothing about the timer. Giving up at a deadline
 * here would be the same mistake in a slower costume -- a stop condition invented by
 * the client, guessing at how long the server is allowed to take, and wrong for the
 * first recording longer than the guess. The only thing entitled to end a wait is a
 * terminal status, and MEMO-16's reaper is what guarantees one arrives: it requeues or
 * fails a memo abandoned in `processing`, which stops this poll the same way finishing
 * would. Until that task lands there is no reaper, so a memo whose worker was killed
 * mid-job is polled at 5s intervals for as long as the tab is open and visible. That
 * is a known and bounded cost -- one localhost request every 5 seconds -- and it is
 * the cheaper half of the trade against a client that stops early on a slow success.
 */
export const HINT_AFTER_MS = 45_000

/**
 * @param {import('vue').Ref<boolean>} active Whether anything is still worth waiting
 *   for. Polling runs exactly while this is true.
 * @param {() => Promise<unknown>} tick One refresh. Its rejections are the caller's
 *   business -- useMemos.load() records its own failures and never throws, which is
 *   what keeps a failed tick from stopping the timer.
 * @returns {{hinting: import('vue').Ref<boolean>}}
 */
export function usePolling(active, tick) {
  /** Whether the wait has passed HINT_AFTER_MS. Rendered by App.vue. */
  const hinting = ref(false)

  /** The pending setTimeout, or null when nothing is scheduled. */
  let timer = null

  /**
   * When the current stretch of non-terminal work began, or 0 when there is none.
   *
   * Wall clock, and deliberately not paused along with the tab: both thresholds are
   * claims about how long the *memo* has been waiting, which does not depend on whether
   * anybody was looking. A tab hidden at 40 seconds and reopened at 300 should show the
   * hint on the first tick, not 5 seconds later.
   */
  let startedAt = 0

  const hidden = () => document.visibilityState === 'hidden'

  const waited = () => Date.now() - startedAt

  function cancel() {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
  }

  function schedule() {
    // Three guards, and the first is the one that matters: run() and
    // onVisibilityChange() can both reach here, so without it a tab regaining focus
    // mid-tick would leave two timers running and double the poll rate for good.
    if (timer !== null || !active.value || hidden()) {
      return
    }

    timer = setTimeout(run, waited() >= DECAY_AFTER_MS ? SLOW_INTERVAL_MS : FAST_INTERVAL_MS)
  }

  async function run() {
    timer = null

    if (!active.value || hidden()) {
      return
    }

    await tick()

    // Re-read rather than waiting for the watcher below to notice. `active` is a
    // computed, so reading it here evaluates it against the list tick() has already
    // replaced, whereas the watcher callback is queued and has not run yet.
    if (!active.value) {
      stop()

      return
    }

    hinting.value = waited() >= HINT_AFTER_MS

    schedule()
  }

  function start() {
    // Only the first of a run of true values sets the clock. `active` can flip false
    // and true again within one stretch of waiting -- submit a second memo while the
    // first is still queued and the list is briefly all-terminal in between -- and
    // restarting the clock there would keep pushing the hint out of reach.
    if (startedAt === 0) {
      startedAt = Date.now()
    }

    schedule()
  }

  function stop() {
    cancel()
    startedAt = 0
    hinting.value = false
  }

  function onVisibilityChange() {
    cancel()

    if (!hidden() && active.value) {
      // Straight to a tick rather than back to schedule(). The list on screen is as
      // stale as the tab was hidden for, and the moment it is looked at again is the
      // one moment another 2s of staleness is actually visible.
      run()
    }
  }

  watch(active, (isActive) => (isActive ? start() : stop()), { immediate: true })

  document.addEventListener('visibilitychange', onVisibilityChange)

  // App.vue is never unmounted, so this is for Vite's HMR -- an edit to this file
  // re-runs setup, and without it every edit would leave another listener and another
  // timer behind on the page being developed against.
  onUnmounted(() => {
    document.removeEventListener('visibilitychange', onVisibilityChange)
    cancel()
  })

  return { hinting }
}
