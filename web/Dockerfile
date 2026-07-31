# syntax=docker/dockerfile:1

# Node 24, the current LTS line, pinned to the major for the same reason the other
# images are: a rebuild must not move the runtime under a lock file. vite 8 requires
# ^20.19 || >=22.12, so 24 is the LTS that satisfies it without being the newest thing
# available.
#
# Alpine because this image has no native build step to worry about: the one native
# dependency in the tree is rolldown's parser, and npm resolves its musl binding from
# the lock file, which carries every platform's entry rather than only the one the
# lock file was generated on.
FROM node:24-alpine

WORKDIR /app

# --- non-root, and the one thing it needs ------------------------------------
#
# The `node` image ships a `node` user at 1000:1000. It is used here rather than the
# 10001 the API and worker share, because that number is the shared-`audio`-volume
# contract (MEMO-12) and this container never mounts that volume -- borrowing the
# number would imply an agreement that does not exist. What matters here is only that
# a dev server executing project code is not root.
#
# /app is chowned before the install so npm runs unprivileged and the tree it writes
# is owned by the user that later has to read it. That ownership is load-bearing under
# compose: /app/node_modules is an anonymous volume, Docker seeds an empty volume from
# the image mount point including its owner, and vite writes its dependency cache to
# node_modules/.vite on every start. Left root-owned, the server dies on EACCES there.
#
# The usual objection to a non-root dev server -- that it has to write into a bind mount
# whose owner is decided by the host -- does not apply, and that was checked rather than
# assumed: with ./web mounted **read-only** the server still starts and still compiles
# .vue files on request. The dependency cache above is the only path it needs to write,
# and that is the volume this image owns.
RUN chown node:node /app
USER node

# Dependencies before source, so editing a component does not reinstall the tree.
#
# `npm ci` rather than `npm install`: it installs exactly the lock file and fails
# instead of silently resolving something new. package-lock.json is committed for that
# reason.
#
# And no `--omit=dev`, which is the opposite of what api/Dockerfile does with composer
# and worth stating: vite is the server this image runs, and it is a devDependency.
COPY --chown=node:node package.json package-lock.json ./
RUN npm ci && npm cache clean --force

# The source. Under compose this is immediately shadowed by the ./web bind mount that
# makes editing possible; it is copied anyway so the image runs on its own under a bare
# `docker run`.
COPY --chown=node:node . .

EXPOSE 5173

# The binary directly, not `npm run dev`. npm would sit between PID 1 and vite for no
# reason, and the reason it matters is shutdown: vite installs its own SIGTERM handler,
# so as PID 1 it stops on `docker compose stop` in well under a second instead of
# waiting out the 10-second kill timeout.
#
# Host and port come from vite.config.js rather than from flags here, so that a bare
# `docker run -p 5173:5173` behaves like the compose service does.
CMD ["./node_modules/.bin/vite"]
