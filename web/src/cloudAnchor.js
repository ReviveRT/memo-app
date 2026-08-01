/*
 * The id the coloured bloom centres itself on.
 *
 * One module for one string, which needs justifying: it used to be a constant inside
 * MemoBackdrop.vue named `recorder-cloud-anchor`, hardcoded a second time in
 * MemoRecorder.vue's template. That was fine while the Record button was the only thing
 * the cloud could sit behind. It is not any more -- the landing page puts the bloom behind
 * its title, where there is no recorder at all -- so the id now has three consumers in two
 * directories and a name that would be a lie in one of them.
 *
 * A `<script setup>` block cannot export anything, so the constant could not have stayed
 * in MemoBackdrop.vue and been imported from there. Hence a module.
 *
 * **Exactly one element with this id may exist at a time.** That is not a convention, it
 * is what `getElementById` means: with two, the backdrop measures whichever comes first in
 * the document and the bloom lands somewhere neither component asked for. The rule holds
 * today because the two consumers are on different routes -- the landing page and the
 * memos page are never mounted together -- and because within the recorder the Record and
 * Stop buttons are mutually exclusive. Anything else that wants to move the cloud should
 * take the id *instead of*, not as well as.
 */
export const CLOUD_ANCHOR_ID = 'cloud-anchor'
