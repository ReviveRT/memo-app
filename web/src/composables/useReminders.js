import { computed, ref } from 'vue'
import { acknowledgeReminder, listPendingReminders } from '../api/memos'
import { applyMemoEverywhere } from './useMemoList'

/*
 * Reminder delivery: what the browser does when a reminder comes due.
 *
 * **Be clear about what this promises, because the name suggests more.** A reminder lives in
 * Postgres and fires from an open tab. There is no service worker and no Web Push, so nothing
 * reaches the user while the app is closed -- that would need VAPID keys, a push endpoint and
 * something server-side holding a schedule, which is a different feature with its own
 * infrastructure. What is here is honest about its edge: while the app is open, reminders
 * arrive on time; while it is not, they wait.
 *
 * **Two delivery paths, and the split is the interesting decision.**
 *
 *   * Due *while the app is open* -> an OS notification plus an in-app banner. The user is
 *     around, possibly in another window, and a notification is what reaches them there.
 *   * Due *while it was closed*, and found overdue on load -> the banner only, no OS
 *     notification. This is the case that makes a single rule wrong: come back on Monday to
 *     eleven overdue reminders and a uniform "always notify" fires eleven system
 *     notifications at once, which is not a reminder, it is a punishment. They are all still
 *     shown, together, in one banner that can be read and dismissed at once.
 *
 * Both paths acknowledge to the API, so a reminder is shown once and not once per page load.
 * `delivered_at` is the record of that, and it lives on the server for the same reason the
 * memo does: the browser is not where the reminder lives.
 *
 * A singleton, because it is a background loop and there is one of it. Started once from
 * MemosView.
 */

/**
 * How often to ask the API what is still owed.
 *
 * This is a *safety net*, not the delivery mechanism. Delivery is a setTimeout aimed at the
 * next due reminder, which is accurate to the second; this poll exists to notice reminders
 * created in another tab, and to recover from the one thing a timer cannot survive -- the
 * machine sleeping, which fires long timers late and without warning.
 *
 * A minute, because that is frequent enough to bound the recovery gap and rare enough to be
 * nothing next to the memo list's two-second poll while anything is transcribing.
 */
const POLL_MS = 60_000

/**
 * The longest a setTimeout is trusted for.
 *
 * setTimeout takes a 32-bit signed millisecond delay -- anything over about 24.8 days
 * overflows and fires *immediately*, which for a reminder set two months out would mean it
 * going off the moment it was created. Capping means a long reminder is simply re-armed on
 * the next tick rather than scheduled once, which the poll above would do anyway.
 *
 * Well under the limit, at an hour, for a second reason: a timer armed for days is a timer
 * that will be wrong by the time it fires, because the machine will have slept. Re-arming
 * hourly keeps every scheduled wait short enough to be plausible.
 */
const MAX_TIMEOUT_MS = 3_600_000

/**
 * How late a reminder can be and still count as "due while we were watching".
 *
 * Without a window, the difference between the two delivery paths would come down to which
 * side of a single millisecond the poll landed on. A reminder that came due nine seconds ago
 * while the tab was open should notify; one that came due while the laptop was shut should
 * not. Thirty seconds is comfortably longer than a poll interval's worth of jitter and far
 * shorter than any real absence.
 */
const FRESHLY_DUE_MS = 30_000

/** @type {import('vue').Ref<Array<object>>} */
const pending = ref([])

/**
 * Reminders that have fired and not yet been dismissed, newest first.
 *
 * Kept client-side only. They are already acknowledged on the server by the time they land
 * here -- this array is just what is still on screen, so a reload clears it, which is right:
 * the user has been told.
 *
 * @type {import('vue').Ref<Array<object>>}
 */
const delivered = ref([])

const permission = ref(notificationPermission())

/** Whether the browser can show OS notifications at all, and whether we may. */
const canNotify = computed(() => permission.value === 'granted')

/**
 * Whether asking for permission is still worth offering.
 *
 * 'denied' is final -- calling requestPermission() again resolves 'denied' without prompting,
 * so a button offering it would do nothing and look broken. Only 'default' is worth a prompt.
 */
const canAskToNotify = computed(() => permission.value === 'default')

let poll = null
let fire = null
let started = false

/** The current permission, or 'unsupported' where the API does not exist. */
function notificationPermission() {
  return typeof Notification === 'undefined' ? 'unsupported' : Notification.permission
}

/**
 * Ask the browser for permission to show notifications.
 *
 * **Called from a control the user pressed, never on load.** An unprompted permission request
 * on page load is the pattern browsers built their heuristics against -- Chrome and Firefox
 * both suppress or auto-deny prompts with no user gesture behind them -- and a denial is
 * permanent, so a badly timed ask does not merely fail, it removes the option. So this is
 * wired to setting a reminder: the moment somebody asks to be reminded is the moment the
 * request makes sense.
 *
 * @returns {Promise<boolean>} Whether notifications may now be shown.
 */
export async function askToNotify() {
  if (typeof Notification === 'undefined') {
    return false
  }

  try {
    permission.value = await Notification.requestPermission()
  } catch {
    // Safari resolved this through a callback rather than a promise until 16, and throws
    // on the promise form. Nothing to do about it and nothing worth saying: the in-app
    // banner is the fallback, and it works either way.
    permission.value = notificationPermission()
  }

  return canNotify.value
}

/**
 * Show one reminder.
 *
 * The banner is unconditional and the OS notification is not, which is the order of
 * preference: the banner is the thing this app controls and can guarantee, and the
 * notification is the thing that reaches somebody in another window.
 *
 * @param {object} reminder
 * @param {boolean} withNotification False for the catch-up path -- see the header.
 */
function show(reminder, withNotification) {
  delivered.value = [reminder, ...delivered.value]

  if (!withNotification || !canNotify.value) {
    return
  }

  try {
    // `tag` set to the reminder id, so a duplicate for the same reminder replaces the
    // previous one rather than stacking. Reachable with two tabs open: both fire, and
    // without a tag the user gets two identical notifications.
    new Notification(reminder.memo_label || 'Memo reminder', {
      body: reminder.note ?? 'Your reminder is due.',
      tag: reminder.id,
    })
  } catch {
    // Constructing a Notification throws on Android Chrome, where it is only allowed from a
    // service worker registration. Swallowed rather than reported: the banner has already
    // been shown, so the user has been told, and an error about a delivery channel they did
    // not choose is noise.
  }
}

/**
 * Tell the API a reminder has been shown.
 *
 * Failures are swallowed on purpose, which is the one place in this app that is true. This
 * runs from a timer with nothing on screen to attach a message to, and the consequence of a
 * lost acknowledgement is mild and self-correcting in the direction that favours the user:
 * the reminder stays pending and is shown again later. Reporting it would put an error banner
 * on the page for a reminder that was, from where the user is standing, delivered.
 */
async function acknowledge(reminder) {
  try {
    // The response is the memo, and it was being thrown away -- which left the alarm badge on
    // the card showing a reminder that had already gone off, until something else happened to
    // reload that list. Nothing usually does: the poll stops once every memo is `ready`, which
    // is exactly the state a memo with a reminder on it is normally in. Handing the memo to the
    // lists is what clears the badge at the moment the toast appears.
    applyMemoEverywhere(await acknowledgeReminder(reminder.id))
  } catch {
    // Deliberately silent. See above.
  }
}

/**
 * Deliver everything due, and arm a timer for the next one that is not.
 *
 * The whole scheduler is this function plus the poll that re-runs it. There is no queue and
 * no per-reminder timer: `pending` is already sorted soonest-first by the API, so the next
 * thing to happen is always the head of the list, and one timer is enough.
 */
function schedule() {
  clearTimeout(fire)
  fire = null

  const now = Date.now()

  /** @type {Array<object>} */
  const due = []

  /** @type {Array<object>} */
  const later = []

  for (const reminder of pending.value) {
    const at = Date.parse(reminder.remind_at)

    // An unparseable timestamp would compare as NaN, which is false against everything --
    // so the reminder would sit in `later` forever and, worse, would be picked as the next
    // one to arm a timer against, producing a NaN delay that fires immediately and loops.
    // Treating it as due gets it shown once and acknowledged, which retires it.
    if (Number.isNaN(at) || at <= now) {
      due.push(reminder)
    } else {
      later.push(reminder)
    }
  }

  for (const reminder of due) {
    const at = Date.parse(reminder.remind_at)

    // The two paths. "Freshly due" means it came due while this loop was running, so the
    // user is here and a notification will reach them. Anything older is a catch-up.
    show(reminder, Number.isNaN(at) || now - at <= FRESHLY_DUE_MS)

    acknowledge(reminder)
  }

  // Dropped from `pending` immediately rather than waiting for the acknowledgement to land
  // and the next poll to confirm it. Without this the next tick would find them still due and
  // show them again -- the poll is a whole minute, which is ample time to fire a reminder
  // sixty times.
  pending.value = later

  if (later.length === 0) {
    return
  }

  const next = Date.parse(later[0].remind_at) - now

  fire = setTimeout(schedule, Math.min(Math.max(next, 0), MAX_TIMEOUT_MS))
}

/**
 * Ask the API what is still owed, then deliver and re-arm.
 *
 * Failures are silent here too, for a milder version of the same reason: this is a background
 * poll, the next one is a minute away, and a reminder that is late because the API was
 * briefly unreachable is not something the user can act on. A broken API is already
 * unmissable -- the memo list's own error banner says so.
 */
async function refresh() {
  try {
    pending.value = await listPendingReminders()
  } catch {
    // Deliberately silent. See above.
  }

  schedule()
}

/**
 * Dismiss one delivered reminder from the banner.
 *
 * Only removes it from the screen -- it was acknowledged when it fired, so there is nothing
 * to tell the server.
 */
export function dismiss(id) {
  delivered.value = delivered.value.filter((reminder) => reminder.id !== id)
}

/** Dismiss all of them, which is what the catch-up case needs after a weekend away. */
export function dismissAll() {
  delivered.value = []
}

/**
 * Start the loop. Idempotent, so mounting the memos view twice does not double it.
 *
 * Deliberately never stopped. It is one timer and one request a minute for the lifetime of
 * the tab, and stopping it on unmount would mean reminders silently not firing whenever the
 * user is on the landing page -- which is a screen they can sit on.
 */
export function startReminders() {
  if (started) {
    return
  }

  started = true

  refresh()

  poll = setInterval(refresh, POLL_MS)
}

/**
 * Re-read the pending set now.
 *
 * Called after a reminder is set or deleted, so the loop schedules against it immediately
 * rather than up to a minute later -- which matters, because the shortest timer the UI offers
 * is five minutes and a whole minute of that would otherwise be spent unaware.
 */
export function refreshReminders() {
  return refresh()
}

export function useReminders() {
  return {
    pending,
    delivered,
    permission,
    canNotify,
    canAskToNotify,

    askToNotify,
    dismiss,
    dismissAll,
    startReminders,
    refreshReminders,
  }
}
