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
        Not "Your Best Memo App". The brief offered that and invited something better, and
        "best" is a claim about the app; this is a claim about what it does for you, which is
        the thing somebody standing on a landing page is deciding about. It also says the two
        words that matter -- speak, and find -- so the sentence doubles as the feature list.
      -->
      <h1 class="landing__title">Say it now.<br />Find it later.</h1>

      <p class="landing__tagline">
        Talk, and your memo is transcribed, titled and searchable. Keep the quick ones loose,
        gather the rest into collections, and set a reminder so the important ones come back
        to you.
      </p>

      <!--
        A RouterLink styled as a button rather than a <button> with a programmatic push. It
        is a navigation, so it should be a real link: middle-click and cmd-click open the app
        in a new tab, the browser shows the target in the status bar, and it works before the
        router's JavaScript has settled. `role` is deliberately not overridden -- it is a
        link and should be announced as one.
      -->
      <RouterLink class="landing__cta" :to="{ name: 'memos' }">Get Started</RouterLink>

      <!--
        Said out loud because it is the unusual thing about this app and it is reassuring
        rather than boastful: nothing here needs an account, and the recordings stay on the
        machine the stack is running on.
      -->
      <p class="landing__note">No account. Runs on your own machine.</p>
    </div>
  </main>
</template>
