import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

/*
 * The Vite dev server is the frontend server for this project -- `docker compose
 * up` runs `vite`, not a built bundle behind nginx. That is what MEMO-07 asks for
 * and it is the right shape for a local-only single-user app: a production build
 * would need its own static server plus a second copy of the /api proxy rule
 * below, written in nginx's configuration language instead of this one, and it
 * would lose HMR. `npm run build` still works if a static host is ever wanted;
 * nothing here depends on the dev server beyond the proxy. MEMO-27 records the
 * trade-off.
 */
export default defineConfig({
  plugins: [vue()],

  server: {
    // Vite binds `localhost` by default, which inside a container is the container's
    // own loopback -- the published port then maps to a socket nothing is listening
    // on. Reproduced by dropping this line, and the detail is worth having: the
    // server comes up on `::1:5173` and *only* there, because Node resolves
    // `localhost` to the IPv6 loopback first. So `curl http://localhost:5173` on the
    // host answers "Recv failure: Connection reset by peer", a wget to 127.0.0.1
    // inside the container is refused outright, and the same wget to [::1] answers
    // 200 -- a server that looks alive from exactly one place nobody looks.
    host: '0.0.0.0',

    // 5173 is already Vite's default; it is spelled out because the compose port
    // mapping and the compose healthcheck both name it.
    //
    // strictPort matters more than it looks. Without it Vite responds to a taken
    // port by listening on the next free one *inside the container*, where nothing
    // reports it -- the host mapping still points at 5173 and the browser gets a
    // connection refused with a running, healthy-looking container behind it. A
    // container almost never has 5173 taken, so this is here to make an
    // otherwise inexplicable failure loud rather than because it is likely.
    port: 5173,
    strictPort: true,

    proxy: {
      // The whole CORS answer. The browser talks to one origin -- the dev server
      // -- and /api/* is forwarded to the API container over the compose network,
      // so there is no cross-origin request to permit and no preflight to answer.
      // api/config/cors.php is the other half of this decision: it switches
      // Laravel's stock wildcard `Access-Control-Allow-Origin: *` off, and names
      // this proxy as the reason it can.
      //
      // No `rewrite`: the API serves its routes under /api itself (apiPrefix in
      // api/bootstrap/app.php), so the path is forwarded unchanged.
      //
      // No `changeOrigin` either, which leaves the forwarded Host as
      // `localhost:5173`. Caddy serves any host under the port-only SERVER_NAME the
      // api image sets, and nothing in the API builds an absolute URL, so the
      // response is identical either way -- and the api sees the Host the browser
      // used rather than one this proxy invented.
      '/api': {
        // Read from the environment because this file also has to work outside
        // Docker: `api` is a compose-network name and resolves nowhere else, so a
        // hardcoded target would make `npm run dev` on a laptop fail on DNS. The
        // default is the compose case, which is the one that has to need no setup;
        // docker-compose.yml sets the variable explicitly anyway so that the
        // service name appears next to the service it names.
        target: process.env.API_PROXY_TARGET || 'http://api:8080',
      },
    },
  },
})
