# Notes

Decisions and trade-offs that the code cannot state for itself.

> **Status: partial.** MEMO-27 owns this file and writes the rest of it. The one
> entry below is here because MEMO-12 has to be decided in exactly one place, and
> two Dockerfiles and a compose file all have to agree with it.

## The shared `audio` volume runs as uid 10001, gid 10001

**Decision.** Every container that mounts the `audio` volume runs as
`10001:10001`, a user named `memo`. `AUDIO_DIR` is `2775` (group-writable,
setgid) and the blobs inside it are `0664`. The numbers live in
`api/Dockerfile` (as `APP_UID` / `APP_GID` build args) and in the
`x-audio-user` anchor in `docker-compose.yml`; `ai/Dockerfile` must create the
same user when it lands (MEMO-08).

**Why it needs deciding at all.** The API writes a blob and the worker reads it
and then deletes it. `unlink(2)` checks write permission on the *directory*, not
on the file — so two containers that disagree about uid/gid produce a worker that
can read every blob and delete none of them. Nothing logs, the memo transcribes
fine, and the volume fills up with files nobody owns. Reproduced before any of
this was written: the worker `cat`s the file and then gets `rm: cannot remove
'/data/audio/a.webm': Permission denied`.

**Why group-writable and not just a matching uid.** A matching uid alone would
work. The setgid directory and the `0664` blobs are the second line: they keep the
arrangement working for a container that runs as some *other* uid with `memo` as a
supplementary group, which is the shape a future ai image is most likely to arrive
in. Both identities were checked container-to-container on a fresh volume — same
uid, and group-only at uid 12345 — and the modes they depend on are pinned by
`api/tests/Unit/SharedAudioVolumeTest.php`.

**One thing MEMO-08 should know.** A file the *worker* creates on this volume
comes out `0644` under the default umask, not `0664`. Nothing today cares, because
the API only ever unlinks one and unlink is permitted by the directory rather than
by the file. A worker that writes a normalized copy the API later has to *modify*
wants `umask(0o002)` or an explicit `chmod`, for the same reason
`LocalAudioStorage` sets its own modes by hand.

**How the mode gets onto the volume.** `api/Dockerfile` creates `AUDIO_DIR` in
the image with the right owner and mode, and Docker copies a named volume's mount
point — uid, gid and mode — out of the image. So the first `docker compose up` on
a clean checkout comes up `drwxrwsr-x memo memo` rather than the `drwxr-xr-x root
root` Docker would otherwise create.

The exact rule matters, and all four cases were checked rather than assumed.
Docker applies the image's owner and mode whenever the volume is **empty** at
container start — not only at creation:

| Volume state | Result |
| --- | --- |
| Never mounted | Seeded from the api image. Correct. |
| Empty, created by a pre-MEMO-12 (root) image | Re-seeded on the next start. Self-heals. |
| Empty, already seeded, then mounted by an image without that path | Left alone. Still correct. |
| Holds blobs from a root-era build | Stays `root:root 0755`. The API cannot write. |

Only the last row needs a human: `docker compose down -v`, or
`docker volume rm memo-app_audio` with the stack down to keep `pgdata`. The
failure is loud — every upload fails on the temp file — so nobody debugs it for
long.

The third row is why the ai image does **not** need to prepare `AUDIO_DIR` and
should not try: an image with no such path leaves an already-seeded volume alone,
but one that creates it with a *different* owner would win the seed whenever it
starts first, and api and ai-worker start concurrently.

**The consequence to know about.** The path is baked into the image, so
overriding `AUDIO_DIR` without rebuilding leaves the volume mounted somewhere
that was never prepared, and the API cannot write there. Change it in both
places or not at all: `.env` and
`docker compose build --build-arg AUDIO_DIR=…`.

**What was rejected.**

- *Leave both containers as root.* This is what the stack did before this task, by
  accident rather than by decision: `dunglas/frankenphp:1-php8.3` and
  `python:3.12-slim` both run as root, so they already agreed. Rejected because
  nothing recorded the agreement, and the two ways of breaking it are not equally
  visible — a `USER` line added to the *worker* breaks only delete, silently, while
  one added to the *api* stops it writing at all and is obvious within a second.
  The quiet half is the one that would have shipped. The API is also the only
  public surface in the stack and the only thing handling multipart uploads; it has
  no business being root.
- *`chown` the volume from an entrypoint.* Needs to start as root and drop
  privileges, which means an entrypoint wrapper and `gosu` in an image that
  currently needs neither.
- *`0777` on the directory.* Removes the need to agree on anything, and removes
  the reason to think about it again. The next person to add a container gets no
  signal at all.

**The one thing that made this more than a two-line change.** FrankenPHP as
non-root does not boot without help. Caddy provisions a local CA on startup even
with a port-only `SERVER_NAME` and no TLS anywhere, and it writes under
`XDG_DATA_HOME` and `XDG_CONFIG_HOME` — which this image sets to `/data` and
`/config`, both root-owned. Unfixed, the container exits before it listens with
`provisioning CA 'local': … mkdir /data/caddy/pki: permission denied`.

Those two directories are chowned in `api/Dockerfile` for that reason.
`bootstrap/cache`, Laravel's `storage/` and Composer's `/config/composer` are
chowned for three unrelated reasons of the same shape — root wrote them during the
build and a non-root process has to write them at runtime — and each carries the
failure it prevents in a comment next to it. Composer's is the least guessable: it
takes its home from `XDG_CONFIG_HOME`, not from `HOME`, so setting `HOME` does not
move it.
