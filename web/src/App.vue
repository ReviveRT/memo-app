<script setup>
import MemoBackdrop from './components/MemoBackdrop.vue'

/*
 * The shell: the backdrop, and whichever view the route names.
 *
 * Everything that used to be in here -- the header, the recorder, the composer, the search
 * box and the list -- moved into views/MemosView.vue, because it is now one of two screens
 * rather than the app. What is left is the one thing that has to outlive a navigation.
 *
 * That thing is the backdrop, and keeping it here rather than in each view is the whole
 * reason this file is not simply a <RouterView>. It owns a canvas, a requestAnimationFrame
 * loop and an AnalyserNode; mounted per view it would tear all of that down and rebuild it
 * on every navigation, so the bloom would blink out and restart from random phases in the
 * middle of pressing Get Started. Mounted here it survives, and the only thing that changes
 * across the transition is where it is centred -- which it re-measures on its own, because
 * the element carrying the anchor id belongs to the view. See cloudAnchor.js.
 */
</script>

<template>
  <!--
    Outside the view and before it, which is both of the things it needs to be: it is
    decoration rather than content, so it does not belong inside any landmark, and the
    layers are fixed with a negative z-index so document order does not decide what
    covers what. It draws nothing this app depends on -- see MemoBackdrop.
  -->
  <MemoBackdrop />

  <!--
    No <transition> around this. A cross-fade between the landing page and the app is the
    obvious flourish and it fights the one effect this design is built around: the bloom is
    a fixed layer that stays put across the navigation, so fading the content over it reads
    as the page dissolving in front of a light that did not move. The cloud gliding to its
    new anchor is the transition.
  -->
  <RouterView />
</template>
