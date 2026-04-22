# Docker cache diagnostics

Build cache is on-disk and content-keyed — it survives reboots, host updates, and upstream base-image changes. When `./run.py` rebuilds from scratch, something cleared or invalidated it.

## Inspect

- `docker buildx du` — build-cache entries with size and last-used age. First thing to run when a rebuild surprises you.
- `docker system df` — overall Docker disk usage; compare against Docker Desktop's 20 GB cap.
- `docker images` — confirms whether `debian:bookworm-slim` and the project image are still present locally.

## Watch a build

- `docker compose -f ./docker-compose.yml build` — run standalone; every step missing the `CACHED` tag is where invalidation happened. The instruction just above that step is the cause.

## Force fresh

- `docker compose build --pull` — re-check Docker Hub for a newer `debian:bookworm-slim`. Use when you want upstream base-image updates to take effect.
- `docker compose build --no-cache` — rebuild every layer from scratch. Only when cache seems corrupt.
- `docker builder prune` — drop build cache, keep images. Next build will be slow.

## Likely culprits when it rebuilds by itself

- BuildKit GC — 48 h ephemeral / 60 d aged / 20 GB Desktop cap. Confirm via `docker buildx du`.
- Docker Desktop auto-updated and reset its VM.
- `HOST_UID` changed between runs — invalidates the `useradd` layer downward.
