import { ref } from 'vue'

/*
 * Which card was just added, so the strip can go to it and make it obvious.
 *
 * **The memo was always first in the list; that was never the problem.** useMemoList.prepend
 * puts a new row at the head of the fast strip the moment the API answers, and it has done
 * since MEMO-06. What it cannot do is bring the strip to the reader. The strip scrolls
 * sideways inside a page that scrolls downwards, so "first" is off the left edge of a
 * container that may itself be off the bottom of the window -- press Save from beside the
 * collections grid and the memo appears somewhere nobody is looking, which is
 * indistinguishable from it not appearing.
 *
 * So there are two halves here and they are separate on purpose. This module holds *which*
 * card is new and for how long; MemoStrip owns the element and does the scrolling, because it
 * is the only thing that has the element. A composable that reached into the DOM would have to
 * be told about a component's refs, and there are two strips on this screen.
 *
 * **One id, not a set.** Two memos submitted a second apart should not both pulse -- the second
 * one is the one that just happened, and two things flashing at once asks the eye to choose.
 * Setting a new id supersedes the old one, which is exactly the behaviour wanted and falls out
 * of the state being a single value.
 */

/**
 * How long the card stays marked as new.
 *
 * Matches the CSS animation -- three pulses at 800ms -- because the class is what runs the
 * animation and removing it early would cut the last one off mid-fade. The two numbers have to
 * agree, and there is nowhere to put one number that both a stylesheet and a module can read,
 * so the stylesheet's rule says this constant's name next to its own duration.
 */
const FLASH_MS = 2_400

/**
 * The id of the memo added most recently, or null.
 *
 * @type {import('vue').Ref<?string>}
 */
const flashedId = ref(null)

let timer = null

/**
 * Mark a memo as just-added.
 *
 * Called from the one write path that creates memos, so a memo that arrived from a poll --
 * written in another tab, or by curl -- does not pulse. That distinction is the point: the
 * animation means "this is the thing you just did", and it stops meaning anything if it also
 * fires for rows the reader had nothing to do with.
 *
 * @param {string} id
 */
export function flashMemo(id) {
  // Cleared and re-set rather than left to run out, so a second memo restarts the animation
  // instead of inheriting whatever was left of the first one's timer.
  clearTimeout(timer)

  flashedId.value = id

  timer = setTimeout(() => {
    // Guarded, because a third memo could have claimed the flash between this timer being
    // scheduled and it firing. Without the check, the newest card would stop pulsing at the
    // moment the *previous* one's timer expired.
    if (flashedId.value === id) {
      flashedId.value = null
    }
  }, FLASH_MS)
}

export function useNewMemoFlash() {
  return { flashedId }
}
