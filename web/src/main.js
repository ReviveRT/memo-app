import { createApp } from 'vue'
import App from './App.vue'
import './styles.css'

/*
 * The whole frontend is this file, App.vue, two components, one composable and one
 * fetch wrapper. What it deliberately does not have, since each absence is a question
 * somebody reading it will otherwise ask:
 *
 *   * No router. There is one screen, and a route would be a URL with nothing on the
 *     other side of it. MEMO-19 adds search to the same screen.
 *   * No Pinia. See the header of composables/useMemos.js.
 *   * No TypeScript. It would add a tsconfig and a type-check step to an app that
 *     computes nothing and whose only external contract is one JSON object -- and that
 *     contract is already enforced where it can be, in the API's typed value objects.
 *     JSDoc marks the two places the shape matters.
 *   * No CSS framework and no component library. The brief says not to polish pixels;
 *     styles.css is one file of plain CSS with a light and a dark palette.
 *   * No client-side persistence, no service worker, no optimistic writes. Postgres is
 *     the only copy of a memo, which is what keeps the list and the database from ever
 *     disagreeing.
 */
createApp(App).mount('#app')
