<script setup>
import { CLOUD_ANCHOR_ID } from '../cloudAnchor'

/*
 * The front door: the bloom, a title, and one button.
 *
 * There is nothing here but markup, and that is the point of the file. The one piece of
 * behaviour a landing page like this would normally carry -- animating the hero -- is
 * already running in MemoBackdrop, one level up in App.vue, and it keeps running across the
 * navigation. All this screen does is tell it where to sit.
 *
 * It does that by carrying CLOUD_ANCHOR_ID on an empty element in the middle of the hero.
 * The backdrop looks that id up and centres on whatever it finds, so no prop is threaded
 * down, no event goes up, and the backdrop needs to know nothing about routes. The memos
 * screen puts the same id on its Record button. See cloudAnchor.js for why only one element
 * may hold it at a time -- which holds here because the two screens are never mounted
 * together.
 *
 * Deliberately not gated on "have you been here before". A landing page that shows itself
 * once and then redirects means `/` behaves differently on the second visit, which makes the
 * URL unreliable and needs somewhere to keep the flag -- and localStorage is the one thing
 * main.js says this app does not have. `/` is the landing page and `/memos` is the app,
 * every time; the router's catch-all sends anything else here because this is the screen
 * with a way forward on it.
 */
</script>

<template>
  <main class="landing">
    <!--
      Empty, aria-hidden, and zero-sized: it exists to be measured. The backdrop reads its
      bounding box and centres the bloom on it, so putting it here rather than on the <h1>
      is what lets the light sit behind the middle of the title block instead of behind the
      first line of text.

      It is positioned by the hero's grid rather than by coordinates, so it follows the
      title when the window changes shape -- which is the whole reason the backdrop measures
      an element instead of taking a percentage.
    -->
    <div :id="CLOUD_ANCHOR_ID" class="landing__anchor" aria-hidden="true"></div>

    <div class="landing__hero">
      <!--
        Three words, one line under them, and a button.

        The paragraph that used to sit here explained transcription, collections and reminders
        before anyone had asked -- three features to read while deciding whether to press one
        button, and they introduce themselves better on the next screen where they can be
        pressed. The "No account. Runs on your own machine." note went with it: true, and the
        unusual thing about this app, but it answers a question nobody has asked yet.

        What replaced them is one line that says what the app *is* rather than what it does.
        It earns its place where a feature list did not, because a title alone leaves "memo"
        doing all the work of explaining the product -- and it is short enough to be read
        without being a thing to read.
      -->
      <h1 class="landing__title">Save Your Memo</h1>

      <p class="landing__subtitle">Your everyday memo app</p>

      <!--
        A RouterLink styled as a button rather than a <button> with a programmatic push. It
        is a navigation, so it should be a real link: middle-click and cmd-click open the app
        in a new tab, the browser shows the target in the status bar, and it works before the
        router's JavaScript has settled. `role` is deliberately not overridden -- it is a
        link and should be announced as one.
      -->
      <RouterLink class="landing__cta" :to="{ name: 'memos' }">Get Started</RouterLink>
    </div>
  </main>
</template>
