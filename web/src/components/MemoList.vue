<script setup>
/*
 * One row per memo, rendering whatever the row already has.
 *
 * Today that is the status, the timestamp and the transcript: `title`, `summary` and
 * `tags` are null or empty on every memo until the worker fills them in (MEMO-08 claims
 * the row, MEMO-21 enriches it). They are rendered behind v-if anyway, on purpose --
 * those tasks then need no change in this file, and the shape of what they produce is
 * visible now rather than being described in a comment somewhere.
 *
 * `duration_ms` and the failure UX are the two exceptions, left out rather than
 * guarded: duration only exists for voice memos (MEMO-10) and a failed memo needs the
 * retry action next to the reason (MEMO-17). Half of either now would be UI those tasks
 * have to undo.
 */
defineProps({
  memos: { type: Array, required: true },

  /**
   * Whether a fresher list is coming -- App.vue passes the composable's `busy`, which spans
   * a pending filter change as well as a request in flight. Named for what this component
   * does with it rather than for its source: it is the difference between "empty because
   * nothing has arrived yet" and "empty because that is the answer".
   */
  loading: { type: Boolean, default: false },

  /**
   * Whether the last load failed. App.vue renders the reason above this component; all
   * this needs to know is that it must not answer a failed GET with "No memos yet",
   * which is a claim about the database that a failed request cannot support.
   */
  failed: { type: Boolean, default: false },

  /**
   * The filter these rows came back for, or null when unfiltered. Only used for the empty
   * state, and for the same reason `failed` exists: "No memos yet" is a claim about the
   * database, and it is false when the memos are there and the filter excluded them. The
   * two empty states also want different actions -- type a memo, or change the filter.
   *
   * `displayedFilter` from the composable, so the sentence names the query that actually
   * returned nothing rather than whatever the box says now -- and so this and MemoSearch's
   * status line cannot disagree about whether a filter is in effect at all. They did: this
   * one used to read the API's echo alone and went on saying "No memos match xylophone"
   * under an emptied box until the unfiltered response landed.
   */
  query: { type: String, default: null },
})

/**
 * The API sends RFC 3339 in UTC with a literal Z -- to_char(created_at AT TIME ZONE
 * 'UTC', ...) in MemoRepository::COLUMNS -- so this is one of the few date strings
 * every browser parses identically, and it is shown in the reader's own zone.
 *
 * The fallback prints the string as it arrived, because "Invalid Date" tells whoever
 * reads it nothing about what came over the wire.
 */
function formatTimestamp(iso) {
  const at = new Date(iso)

  return Number.isNaN(at.getTime()) ? iso : at.toLocaleString()
}
</script>

<template>
  <!--
    Five states. Three of them are the reason `loading`, `failed` and `query` are props at
    all: an empty list because the first GET is still in flight, an empty list because the
    GET failed, and an empty list because a filter excluded everything are all different
    from having no memos -- and answering any of them with "No memos yet" is a small lie
    that reads as a bug.
  -->
  <p v-if="loading && memos.length === 0" class="notice">Loading…</p>

  <ul v-else-if="memos.length > 0" class="memos">
    <li v-for="memo in memos" :key="memo.id" class="memo">
      <div class="memo__meta">
        <span class="badge" :class="`badge--${memo.status}`">{{ memo.status }}</span>
        <span class="badge badge--source">{{ memo.source }}</span>
        <time :datetime="memo.created_at">{{ formatTimestamp(memo.created_at) }}</time>
      </div>

      <h2 v-if="memo.title" class="memo__title">{{ memo.title }}</h2>

      <p v-if="memo.summary" class="memo__summary">{{ memo.summary }}</p>

      <!--
        white-space: pre-wrap in the stylesheet, so the newlines somebody typed into
        the textarea survive. Interpolation escapes the text, so a memo containing
        markup is shown, not run.
      -->
      <p v-if="memo.transcript" class="memo__transcript">{{ memo.transcript }}</p>

      <p v-else class="memo__transcript memo__transcript--empty">No transcript yet.</p>

      <!--
        Keyed by position, not by the tag itself. Nothing guarantees these are unique --
        `tags` is a plain text[] with no constraint and MEMO-21 fills it from model
        output -- and a repeated tag would key two nodes the same, which Vue reports as
        "Duplicate keys found during update" in the console. The list is short,
        append-only and never reordered, so the index is a stable key here.
      -->
      <ul v-if="memo.tags?.length" class="tags">
        <li v-for="(tag, position) in memo.tags" :key="position" class="tags__tag">{{ tag }}</li>
      </ul>
    </li>
  </ul>

  <!--
    The filter came back empty. Quoting it matters: a typo is the likeliest reason, and a
    message that does not repeat what was searched for gives nothing to spot it in.
  -->
  <p v-else-if="query !== null && !failed" class="notice">
    No memos match <strong>{{ query }}</strong
    >. Try fewer words, or clear the filter.
  </p>

  <p v-else-if="!failed" class="notice">No memos yet. Type one above.</p>
</template>
