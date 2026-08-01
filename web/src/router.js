import { createRouter, createWebHistory } from 'vue-router'
import LandingView from './views/LandingView.vue'
import MemosView from './views/MemosView.vue'

/*
 * Two routes, which is the whole of the routing this app has.
 *
 * main.js used to say "No router. There is one screen, and a route would be a URL with
 * nothing on the other side of it." That was true and is not any more: there is a landing
 * page and there is the app, and the second one is where every existing feature lives. The
 * note in main.js has been rewritten rather than deleted, because the *reason* it gave is
 * still the test any future dependency has to pass.
 *
 * `createWebHistory`, not `createWebHashHistory`. Real paths, so /memos can be bookmarked
 * and the back button steps between the two screens rather than out of the site. It needs
 * the server to answer an unknown path with index.html, and Vite's dev server does that by
 * default -- its appType is 'spa', so the history fallback is already there and there is
 * nothing to configure in vite.config.js. Worth knowing if this is ever put behind a static
 * host instead: that host needs the same fallback, or a reload on /memos is a 404. MEMO-27
 * records that the dev server *is* the frontend server here.
 *
 * Both components are imported eagerly rather than lazily. A dynamic import per route is
 * the usual advice and it would be wrong at this size: the two views together are smaller
 * than the router, the app is served from localhost, and a lazy landing page means a blank
 * screen for one round trip on the one screen whose entire job is to look good immediately.
 */
const routes = [
  {
    path: '/',
    name: 'landing',
    component: LandingView,
  },

  {
    path: '/memos',
    name: 'memos',
    component: MemosView,
  },

  /*
   * Anything else goes to the landing page rather than to a 404 screen.
   *
   * There are two real paths, so an unknown one is a typo or a stale bookmark, and a
   * dedicated "not found" view would be a third screen built for a case with nothing to
   * recover. Landing is the recovery: it has the one button that leads somewhere.
   *
   * The path pattern needs the `(.*)*` repeat rather than a plain `:catchAll(.*)`, or a
   * path with slashes in it does not match.
   */
  {
    path: '/:catchAll(.*)*',
    redirect: { name: 'landing' },
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,

  /*
   * Every navigation starts at the top.
   *
   * Without this, vue-router leaves the scroll position alone, so arriving at /memos from a
   * landing page the user had scrolled down keeps that offset -- and the memo list starts
   * mid-page for no visible reason. `savedPosition` is honoured so that going *back* returns
   * to where the reader was, which is the one case where keeping the offset is what they
   * meant.
   */
  scrollBehavior(to, from, savedPosition) {
    return savedPosition ?? { top: 0 }
  },
})
