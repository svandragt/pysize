# How it works

pysize answers one question — *how big is this package, with everything it pulls
in?* — using only the public PyPI JSON API.

## Resolution

For a requirement like `headroom-ai[all]` the resolver (`resolve()` in
`main.py`) runs a fixpoint over the dependency graph:

1. **Track each package once.** State is keyed by canonical package name, so a
   package reached by many paths is counted a single time. This is what makes
   the total a real *deduplicated* size rather than a sum-with-duplicates.

2. **Accumulate extras and version constraints.** For every package it keeps the
   **union of requested extras** and the **intersection of all version
   specifiers** that reach it. It then selects the highest released version
   satisfying that combined specifier (`best_version()`).

3. **Read the chosen version's own metadata.** The top-level PyPI JSON only
   carries the *latest* version's `requires_dist`. To honor specifiers we fetch
   the per-version endpoint (`/pypi/{name}/{version}/json`) for the version we
   actually picked, and expand *its* dependencies.

4. **Re-expand only on change.** A package is re-processed only when its chosen
   version or its extra set grows. Because extras only accumulate and specifiers
   only tighten, this terminates — and it handles self-referential extras like
   `pkg[all]` → `pkg[eval]` cleanly instead of treating them as a cycle to cut.

### Environment markers

Dependencies gated on markers (`; python_version < "3.13"`,
`; extra == "all"`, platform markers) are evaluated against a **fixed target
Python** (`TARGET_PYTHON = "3.12"`), not the interpreter running the server.
This keeps results deterministic regardless of host — otherwise a dependency
could appear or vanish depending on which Python happens to run the service.

### Size

Each package contributes the size of one distribution file — preferring the
`py3-none-any` wheel, then any wheel, then an sdist (`pick_distribution()`).

## Caching

Two layers, both with a 1-hour TTL:

- **HTTP cache** — every PyPI response (index + per-version) is cached. Backed by
  a **two-tier store**: a size-bounded in-memory LRU (32 MB of JSON text) in
  front of a **SQLite file**. The LRU keeps hot packages instant; SQLite bounds
  memory (a single botocore index is ~3 MB of JSON → ~20–30 MB parsed, so you
  can't hold many in RAM) and persists across restarts so redeploys stay warm.
- **Resolution cache** — the full computed result is cached per normalized
  requirement (`resolve:name[extras]specifier`), so a repeat query skips the
  graph walk entirely (~1 ms instead of ~115 ms for a large graph).

SQLite I/O runs in a worker thread so it never blocks the event loop. A
background sweep evicts expired rows; failures and 404s get a short negative
cache.

### Politeness

- A single shared `httpx.AsyncClient` (connection reuse) for the app's lifetime.
- A semaphore caps outbound PyPI requests at 12, so resolving a 174-package
  graph doesn't open hundreds of connections at once.
- Single-flight de-duplication: concurrent requests for the same URL share one
  fetch.

## Request flow

```
GET /api/size?pkg=flask
  → resolution cache hit?  → return (~1 ms)
  → resolve():
      fixpoint over the graph, each fetch via the HTTP cache
      (memory LRU → SQLite → PyPI, capped at 12 concurrent)
  → sum unique wheel sizes, sort desc
  → cache result, return JSON
```
