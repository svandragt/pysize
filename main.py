# /// script
# requires-python = ">=3.10"
# dependencies = ["fastapi", "uvicorn", "httpx", "packaging"]
# ///
"""Pysize — a bundlephobia-style size explorer for PyPI packages."""

import asyncio
import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

CACHE_TTL = 3600          # seconds to keep a successful PyPI response
NEG_TTL = 60              # seconds to keep a failure/404 (negative cache)
CACHE_PATH = Path(os.environ.get("PYSIZE_CACHE") or Path(__file__).with_name("pysize-cache.sqlite"))
MAX_CONCURRENCY = 12      # simultaneous outbound requests to PyPI
MEM_BUDGET = 32 * 1024 * 1024  # bytes of cached JSON text kept in memory
PURGE_INTERVAL = 600      # seconds between expired-row sweeps
USER_AGENT = "pysize-poc (+https://github.com/; package-size explorer)"

# Evaluate environment markers against a fixed target Python, not whatever
# interpreter happens to run the server — otherwise results drift (e.g. a
# `python_version < "3.13"` dep appears or vanishes based on the host).
TARGET_PYTHON = "3.12"
_TARGET_ENV = {
    **default_environment(),
    "python_version": TARGET_PYTHON,
    "python_full_version": TARGET_PYTHON + ".0",
}

_MISS = object()  # distinguishes "absent/expired" from a cached None


class Cache:
    """Two-tier TTL cache: a size-bounded in-memory LRU over a SQLite file.

    Values must be JSON-serializable. A stored value may be None (negative
    cache); get() returns the _MISS sentinel only when a key is absent or
    expired. SQLite work runs in a worker thread so it never blocks the loop.
    Memory is bounded by serialized-text bytes, so a few thousand huge package
    indexes can be cached on disk without ever holding them all in RAM.
    """

    def __init__(self, path: Path, mem_budget: int = MEM_BUDGET):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS cache "
            "(key TEXT PRIMARY KEY, body TEXT, expires REAL)"
        )
        self._db.commit()
        self._lock = threading.Lock()
        self._mem: OrderedDict[str, tuple[float, object, int]] = OrderedDict()
        self._mem_bytes = 0
        self._mem_budget = mem_budget

    # --- in-memory tier (touched only from the event-loop thread) ---
    def _mem_get(self, key: str, now: float):
        hit = self._mem.get(key)
        if hit is None:
            return _MISS
        exp, val, _ = hit
        if exp <= now:
            self._mem_drop(key)
            return _MISS
        self._mem.move_to_end(key)
        return val

    def _mem_drop(self, key: str):
        old = self._mem.pop(key, None)
        if old:
            self._mem_bytes -= old[2]

    def _mem_put(self, key: str, exp: float, val: object, nbytes: int):
        self._mem_drop(key)
        self._mem[key] = (exp, val, nbytes)
        self._mem_bytes += nbytes
        while self._mem_bytes > self._mem_budget and self._mem:
            k, (_, _, nb) = self._mem.popitem(last=False)
            self._mem_bytes -= nb

    # --- sqlite tier (run via asyncio.to_thread) ---
    def _db_get(self, key: str, now: float):
        with self._lock:
            row = self._db.execute(
                "SELECT body, expires FROM cache WHERE key=?", (key,)
            ).fetchone()
        if row is None or row[1] <= now:
            return _MISS
        return row[0], row[1]

    def _db_put(self, key: str, body: str, exp: float):
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO cache (key, body, expires) VALUES (?,?,?)",
                (key, body, exp),
            )
            self._db.commit()

    def _db_purge(self):
        with self._lock:
            self._db.execute("DELETE FROM cache WHERE expires < ?", (time.time(),))
            self._db.commit()

    async def get(self, key: str):
        now = time.time()
        val = self._mem_get(key, now)
        if val is not _MISS:
            return val
        res = await asyncio.to_thread(self._db_get, key, now)
        if res is _MISS:
            return _MISS
        body, exp = res
        val = json.loads(body)
        self._mem_put(key, exp, val, len(body))
        return val

    async def set(self, key: str, val: object, ttl: int):
        body = json.dumps(val)
        exp = time.time() + ttl
        self._mem_put(key, exp, val, len(body))
        await asyncio.to_thread(self._db_put, key, body, exp)

    async def purge(self):
        await asyncio.to_thread(self._db_purge)

    def close(self):
        with self._lock:
            self._db.close()


# Module-level singletons, initialized in the lifespan handler.
cache: Cache
sem: asyncio.Semaphore
# In-flight de-duplication: URL -> Future, so concurrent callers share one fetch.
_inflight: dict[str, asyncio.Future] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global cache, sem
    cache = Cache(CACHE_PATH)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    app.state.client = httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=20)

    async def purge_loop():
        while True:
            await asyncio.sleep(PURGE_INTERVAL)
            await cache.purge()

    purger = asyncio.create_task(purge_loop())
    try:
        yield
    finally:
        purger.cancel()
        with suppress(asyncio.CancelledError):
            await purger
        await app.state.client.aclose()
        cache.close()


app = FastAPI(lifespan=lifespan)


async def get_json(client: httpx.AsyncClient, url: str) -> dict | None:
    """GET JSON via the shared cache, single-flight, and concurrency cap."""
    cached = await cache.get(url)
    if cached is not _MISS:
        return cached
    if url in _inflight:
        return await _inflight[url]

    fut = asyncio.get_running_loop().create_future()
    _inflight[url] = fut
    try:
        try:
            async with sem:  # cap simultaneous PyPI requests
                r = await client.get(url)
            r.raise_for_status()
            data = r.json()
            await cache.set(url, data, CACHE_TTL)
        except Exception:
            data = None
            await cache.set(url, None, NEG_TTL)  # brief negative cache
        fut.set_result(data)
        return data
    finally:
        _inflight.pop(url, None)


async def get_index(client: httpx.AsyncClient, name: str) -> dict | None:
    """All releases of a package (versions + files), for version selection."""
    return await get_json(client, f"https://pypi.org/pypi/{name}/json")


async def get_version_meta(client: httpx.AsyncClient, name: str, version: str) -> dict | None:
    """Metadata for one specific version — carries that version's requires_dist."""
    return await get_json(client, f"https://pypi.org/pypi/{name}/{version}/json")


def best_version(index: dict, spec: SpecifierSet) -> str | None:
    """Highest released version satisfying `spec`.

    Skips versions with no files or fully yanked. Falls back to the highest
    available version if the specifier is unsatisfiable (a POC compromise —
    a real resolver would backtrack or error).
    """
    candidates = []
    for v, files in index.get("releases", {}).items():
        if not files or all(f.get("yanked") for f in files):
            continue
        try:
            Version(v)
        except InvalidVersion:
            continue
        candidates.append(v)
    if not candidates:
        return None
    allowed = list(spec.filter(candidates)) or candidates
    return max(allowed, key=Version)


def pick_distribution(release_files: list[dict]) -> dict | None:
    """Choose the best file to represent download size for a release.

    Prefer a pure-python wheel, then any wheel, then an sdist.
    """
    wheels = [f for f in release_files if f.get("packagetype") == "bdist_wheel"]
    pure = [f for f in wheels if f.get("filename", "").endswith("py3-none-any.whl")]
    if pure:
        return pure[0]
    if wheels:
        return wheels[0]
    sdists = [f for f in release_files if f.get("packagetype") == "sdist"]
    return sdists[0] if sdists else (release_files[0] if release_files else None)


def marker_allows(req: Requirement, extras: set[str]) -> bool:
    """Is this dependency active given the requested extras?

    A dep with no marker is always active. A dep gated on `extra == "x"`
    is active only if "x" was requested.
    """
    if req.marker is None:
        return True
    # Evaluate against each requested extra plus the base (no-extra) case;
    # include if any matches. Markers that don't reference `extra` evaluate
    # the same regardless, so the base case covers ordinary platform markers.
    for e in {""} | set(extras):
        if req.marker.evaluate({**_TARGET_ENV, "extra": e}):
            return True
    return False


async def resolve(
    client: httpx.AsyncClient,
    root: Requirement,
) -> dict[str, dict]:
    """Resolve the dependency set as a fixpoint, honoring version specifiers.

    Each package is tracked once (fixes double-counting). For every package we
    accumulate the union of requested extras and the intersection of all version
    specifiers reaching it, then pick the highest satisfying version. A package
    is re-expanded only when its chosen version or its extra set changes — so
    self-references like `pkg[all]` -> `pkg[eval]` union cleanly and terminate.

    Returns canonical name -> {name, version, size}.
    """
    root_canon = canonicalize_name(root.name)
    name_for = {root_canon: root.name}
    node_spec: dict[str, SpecifierSet] = {}
    node_extras: dict[str, set[str]] = {}
    processed: dict[str, tuple] = {}        # canon -> (version, frozenset(extras)) last expanded
    info: dict[str, dict] = {}              # canon -> {name, version, size}
    pending: dict[str, tuple[SpecifierSet, set[str]]] = {
        root_canon: (root.specifier, set(root.extras))
    }

    while pending:
        # Merge incoming constraints into each node's accumulated state.
        nodes = list(pending)
        for canon, (spec, extras) in pending.items():
            node_spec[canon] = node_spec.get(canon, SpecifierSet()) & spec
            node_extras[canon] = node_extras.get(canon, set()) | extras
        pending = {}

        indexes = await asyncio.gather(*(get_index(client, name_for[c]) for c in nodes))

        # Choose a version for each node; expand only those whose (version, extras) changed.
        to_expand = []
        for canon, index in zip(nodes, indexes):
            if index is None:
                continue
            version = best_version(index, node_spec[canon])
            if version is None:
                continue
            sig = (version, frozenset(node_extras[canon]))
            if processed.get(canon) == sig:
                continue
            processed[canon] = sig
            dist = pick_distribution(index.get("releases", {}).get(version, []))
            info[canon] = {
                "name": index["info"]["name"],
                "version": version,
                "size": dist.get("size", 0) if dist else 0,
            }
            to_expand.append(canon)

        metas = await asyncio.gather(
            *(get_version_meta(client, name_for[c], info[c]["version"]) for c in to_expand)
        )
        for canon, meta in zip(to_expand, metas):
            if meta is None:
                continue
            for raw in meta["info"].get("requires_dist") or []:
                try:
                    req = Requirement(raw)
                except Exception:
                    continue
                if not marker_allows(req, node_extras[canon]):
                    continue
                dep = canonicalize_name(req.name)
                name_for.setdefault(dep, req.name)
                spec, extras = pending.get(dep, (SpecifierSet(), set()))
                pending[dep] = (spec & req.specifier, extras | set(req.extras))

    return info


@app.get("/api/size")
async def api_size(request: Request, pkg: str):
    try:
        req = Requirement(pkg.strip())
    except Exception:
        return JSONResponse({"error": f"Could not parse '{pkg}'"}, status_code=400)

    # Cache the whole resolution, not just the upstream HTTP responses, so a
    # repeated query skips the graph walk entirely.
    key = f"resolve:{canonicalize_name(req.name)}[{','.join(sorted(req.extras))}]{req.specifier}"
    cached = await cache.get(key)
    if cached not in (_MISS, None):
        return cached

    info = await resolve(request.app.state.client, req)

    root = canonicalize_name(req.name)
    root_pkg = info.get(root)
    if root_pkg is None:
        return JSONResponse({"error": f"Package '{req.name}' not found"}, status_code=404)

    # Sum each unique package once; build a flat list of dependencies by size.
    deps = [
        {"name": p["name"], "version": p["version"], "size": p["size"]}
        for canon, p in info.items()
        if canon != root
    ]
    deps.sort(key=lambda d: d["size"], reverse=True)
    total = root_pkg["size"] + sum(d["size"] for d in deps)

    result = {
        "name": root_pkg["name"],
        "version": root_pkg["version"],
        "self_size": root_pkg["size"],
        "total_size": total,
        "dep_count": len(deps),
        "packages": deps,
    }
    await cache.set(key, result, CACHE_TTL)
    return result


@app.get("/", response_class=HTMLResponse)
async def index():
    return Path(__file__).with_name("index.html").read_text()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8731, log_level="warning")
