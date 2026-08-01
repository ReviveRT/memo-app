import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router'
import './styles.css'

/*
 * The frontend is this file, App.vue, two views, a dozen components, a handful of
 * composables and two fetch wrappers. What it deliberately does not have, since each
 * absence is a question somebody reading it will otherwise ask:
 *
 *   * No Pinia. See the header of composables/useMemos.js.
 *   * No TypeScript. It would add a tsconfig and a type-check step to an app whose only
 *     external contract is a handful of JSON objects -- and that contract is already
 *     enforced where it can be, in the API's typed value objects. JSDoc marks the places
 *     the shape matters.
 *   * No CSS framework and no component library. The brief says not to polish pixels;
 *     styles.css is one file of plain CSS with a light and a dark palette.
 *   * No client-side persistence, no service worker, no optimistic writes. Postgres is
 *     the only copy of a memo, which is what keeps the list and the database from ever
 *     disagreeing. The one thing that looks like an exception -- reminders firing in the
 *     browser -- is not: the reminder lives in Postgres and the browser only decides when
 *     to say something about it. See composables/useReminders.js for what that does and
 *     does not promise.
 *
 * **There is now a router, and this note used to say there was not.** The reason it gave
 * was "there is one screen, and a route would be a URL with nothing on the other side of
 * it" -- true of a single-screen app, and no longer true once there was a landing page.
 * Two screens that both deserve a URL is the case a router is for, so the dependency was
 * taken rather than hand-rolled over the History API: the forty-odd lines that would have
 * replaced it are forty lines to own and test, and they would have reimplemented scroll
 * restoration and the back button worse. The over-engineering test the old note applied has
 * not been relaxed -- vue-router passes it, and Pinia and TypeScript above still do not.
 *
 * The router is installed before mount, which is what lets App.vue render a <RouterView>
 * on the first frame rather than after a tick.
 */
createApp(App).use(router).mount('#app')
