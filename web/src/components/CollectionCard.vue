<script setup>
import { nextTick, ref } from 'vue'

/*
 * One collection, as a card in the grid: its name, how full it is, and a glimpse of what is
 * inside.
 *
 * The three labels under the name are the "recent notes" the reference design shows, and they
 * come from the API already chosen and already truncated -- CollectionRepository picks the
 * best short thing each of the three newest memos has, in SQL, so the grid is one query rather
 * than one per card and no transcripts cross the wire to render three single lines.
 *
 * A fixed height, set in styles.css rather than here. It is what makes "no more than three
 * rows, then scroll" expressible at all: the grid's max-height is three of these plus two
 * gaps, and a card that grew with its content would make that number a guess.
 */

const props = defineProps({
  collection: { type: Object, required: true },

  /** True while a create, rename or delete is in flight -- shared across the whole grid. */
  saving: { type: Boolean, default: false },
})

const emit = defineEmits(['open', 'rename', 'delete'])

/** Whether this card is showing its rename field instead of its name. */
const renaming = ref(false)
const draft = ref('')
const nameEl = ref(null)

/**
 * Start renaming, with the current name already in the box and selected.
 *
 * Pre-filled because a rename is usually an edit rather than a replacement -- "Work" becoming
 * "Work 2026" -- and selected because the other common case is replacing it outright, which
 * then costs one keystroke instead of a select-all.
 *
 * The API treats submitting the unchanged name as a successful no-op rather than a duplicate,
 * which is what makes pre-filling safe: the unique index compares the row against itself.
 */
async function startRename() {
  draft.value = props.collection.name
  renaming.value = true

  // After the field exists. Focus is moved deliberately rather than with `autofocus`, which
  // only applies on page load and does nothing for an element revealed by a click.
  await nextTick()
  nameEl.value?.select()
}

function submitRename() {
  const next = draft.value.trim()

  // Nothing typed is a cancel rather than an error: the API would refuse a blank name, and
  // answering that with a 422 for something the user clearly did not mean is worse than
  // closing the field.
  if (next === '') {
    renaming.value = false

    return
  }

  emit('rename', props.collection.id, next)
  renaming.value = false
}

function cancelRename() {
  renaming.value = false
}

/**
 * Confirm, then delete.
 *
 * The wording says what *survives*, because that is what somebody hesitating over this button
 * is unsure about. Deleting a collection does not delete its memos -- `ON DELETE SET NULL` on
 * `memos.collection_id` returns them to the fast strip -- and a bare "Are you sure?" leaves
 * the reader to assume the worse of the two possibilities and not press it.
 *
 * window.confirm rather than a bespoke dialog: the question is one line, and this card is
 * already reachable from inside a screen that has two modals. A third would be one too many
 * for a yes/no.
 *
 * In the script rather than inline in the template, because `window` is not one of the globals
 * Vue exposes to template expressions -- the allowlist is Math, Date, JSON and friends -- so
 * the inline version would fail to resolve rather than merely being hard to read.
 */
function confirmDelete() {
  const memos =
    props.collection.memo_count === 1 ? 'Its 1 memo stays' : `Its ${props.collection.memo_count} memos stay`

  const question =
    props.collection.memo_count === 0
      ? `Delete “${props.collection.name}”?`
      : `Delete “${props.collection.name}”? ${memos} — they go back to fast memos.`

  if (window.confirm(question)) {
    emit('delete', props.collection.id)
  }
}
</script>

<template>
  <article class="collection">
    <!--
      Not a <button> wrapping the whole card, unlike a memo card. This one has controls inside
      it -- rename, delete -- and a button cannot contain other buttons: the HTML is invalid
      and browsers recover from it by hoisting the inner buttons out, which breaks the layout
      in ways that differ per engine. So the *name* is the button that opens it.
    -->
    <header class="collection__head">
      <form v-if="renaming" class="collection__rename" @submit.prevent="submitRename">
        <label>
          <span class="sr-only">Collection name</span>
          <!--
            @keydown.esc rather than relying on a Cancel button alone: Escape is what people
            press to abandon an inline edit, and without it the only way out is to restore the
            original text by hand.
          -->
          <input
            ref="nameEl"
            v-model="draft"
            type="text"
            maxlength="120"
            :disabled="saving"
            @keydown.esc="cancelRename"
          />
        </label>

        <button type="submit" :disabled="saving">Save</button>
        <button type="button" class="ghost" :disabled="saving" @click="cancelRename">
          Cancel
        </button>
      </form>

      <template v-else>
        <button type="button" class="collection__name" @click="emit('open', collection)">
          {{ collection.name }}
        </button>

        <span class="collection__count">
          {{ collection.memo_count === 1 ? '1 memo' : `${collection.memo_count} memos` }}
        </span>
      </template>
    </header>

    <!--
      The glimpse. Keyed by position rather than by the label, because two memos can perfectly
      well have the same title -- "Standup" every morning -- and duplicate keys are a Vue
      warning and a patching bug rather than a cosmetic one.
    -->
    <ul v-if="collection.recent_labels.length" class="collection__recent">
      <li v-for="(label, at) in collection.recent_labels" :key="at">{{ label }}</li>
    </ul>

    <p v-else class="collection__empty">
      Nothing in here yet. Open a memo and file it from its card.
    </p>

    <footer v-if="!renaming" class="collection__actions">
      <button type="button" class="ghost" :disabled="saving" @click="startRename">Rename</button>

      <!-- Confirms first, and the wording is the point -- see confirmDelete. -->
      <button type="button" class="ghost ghost--danger" :disabled="saving" @click="confirmDelete">
        Delete
      </button>
    </footer>
  </article>
</template>
