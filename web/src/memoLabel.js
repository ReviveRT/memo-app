/*
 * The short thing that names a memo on a card.
 *
 * The brief asks for "a title of a summary of each memo, for example -> Sunday Meeting" --
 * something brief enough to scan a strip of them and know which is which. That is exactly
 * what the enrichment pass produces (MEMO-21 writes `title`), so most of the time this
 * function does nothing but return it.
 *
 * The rest of the time is what it exists for. A memo has no title for the whole window
 * between being recorded and being enriched, and that window is precisely when the user is
 * looking at it -- they just made it. So the fallbacks matter more than the happy path:
 *
 *   * `title` -- the enriched, deliberate one. "Sunday Meeting".
 *   * `summary` -- also enriched, and present in the same responses as `title`, so this is
 *     only reached if a future enrichment writes one without the other.
 *   * the first line of the transcript -- what a text memo has immediately, and what a voice
 *     memo has as soon as it is transcribed.
 *   * a wait, or a failure. A voice memo has *nothing* until the worker gets to it, and
 *     "Untitled memo" for a row that is visibly working reads as a result rather than as a
 *     wait.
 *
 * The API has its own copy of this rule in SQL -- CollectionRepository's card labels and the
 * reminder list's `memo_label` both coalesce title, summary, transcript. They agree on the
 * first three and cannot agree on the last two: SQL has no access to a per-row notion of
 * "still working" that reads well in a notification, and a notification saying "Transcribing…"
 * would be useless. Worth knowing that the two exist rather than assuming one is the source.
 */

/** How much of a transcript can stand in for a title before it stops being a label. */
const MAX_LABEL_LENGTH = 60

/**
 * @param {object} memo
 * @returns {string} Always something renderable -- never empty, never null.
 */
export function memoLabel(memo) {
  if (typeof memo?.title === 'string' && memo.title.trim() !== '') {
    return memo.title.trim()
  }

  if (typeof memo?.summary === 'string' && memo.summary.trim() !== '') {
    return truncate(memo.summary.trim())
  }

  if (typeof memo?.transcript === 'string' && memo.transcript.trim() !== '') {
    // The first line, not the first 60 characters. A typed memo often opens with its own
    // heading, and cutting mid-sentence when a natural break was two words earlier makes the
    // strip look like it is guessing.
    const [firstLine] = memo.transcript.trim().split('\n')

    return truncate(firstLine)
  }

  // Nothing to show yet, and which nothing it is decides the wording. `failed` gets its own,
  // because "Transcribing…" on a memo that stopped transcribing an hour ago is a lie the user
  // will wait on.
  if (memo?.status === 'failed') {
    return 'Could not transcribe'
  }

  return memo?.status === 'ready' ? 'Empty memo' : 'Transcribing…'
}

/** An ellipsis rather than a hard cut, so a truncated label reads as truncated. */
function truncate(text) {
  return text.length <= MAX_LABEL_LENGTH ? text : `${text.slice(0, MAX_LABEL_LENGTH - 1).trimEnd()}…`
}
