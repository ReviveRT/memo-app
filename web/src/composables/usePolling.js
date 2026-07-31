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
 * first recording longer than the guess. Only a terminal status ends a wait.
 *
 * Which makes ending it the server's business, and the guarantee there is narrower than
 * it first looks. MEMO-16's reaper covers one case: a memo abandoned in `processing`
 * past its lease, which it requeues or fails, stopping this poll the same way finishing
 * would. It does not cover a memo sitting in `queued` because no worker is running at
 * all -- both replicas stopped, or never started -- and nothing does. That one waits for
 * a worker, however long that takes, so the open-ended case survives MEMO-16 rather than
 * being closed by it.
 *
 * The cost of the open-ended case is one localhost request every 5 seconds while the tab
 * is open and visible, and it is the cheaper half of the trade: the other half is a
 * client that stops early on a slow success, which is the failure this whole task exists
 * to prevent.
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
    // One clock per stretch of waiting, set unconditionally. `watch` fires only on a
    // change and every false transition runs stop(), which clears it -- so this is
    // never reached with a clock already running, and a guard against restarting one
    // would be dead code dressed as caution.
    //
    // Note what the stretch is measured from: the moment the page went from all-terminal
    // to not, which is not the moment any particular memo started waiting. Two memos
    // queued a minute apart share one clock, and the hint fires 45s after the first of
    // them. Per-memo timing would have to trust `created_at` against the browser's own
    // clock, and the two belong to different machines.
    startedAt = Date.now()

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
