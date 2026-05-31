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

CACHE_TTL = 3600          # seconds a successful PyPI response stays fresh
STALE_TTL = 86400         # extra seconds a stale PyPI response is served while it revalidates
# A published version's metadata is immutable (only a rare yank flag can flip),
# so the per-version endpoint stays fresh for a week and serves stale for a month.
VERSION_TTL = 7 * 86400
VERSION_STALE_TTL = 30 * 86400
NEG_TTL = 60              # seconds to keep a failure/404 (negative cache)
CACHE_PATH = Path(os.environ.get("PYSIZE_CACHE") or Path(__file__).with_name("pysize-cache.sqlite"))
MAX_CONCURRENCY = 12      # simultaneous outbound requests to PyPI
MAX_PACKAGES = 800        # stop expanding past this many unique packages (abuse guard)
TOP_PACKAGES_COUNT = 24   # heaviest PyPI projects shown as explore-me chips on the home page
RESOLVE_TIMEOUT = 45      # seconds; bounded below nginx's proxy_read_timeout
MEM_BUDGET = 32 * 1024 * 1024  # bytes of cached JSON text kept in memory
PURGE_INTERVAL = 600      # seconds between expired-row sweeps
USER_AGENT = "pysize/1.0 (+https://github.com/svandragt/pysize; https://pysize.vandragt.com)"

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

    Each entry carries two timestamps: `fresh` (when the value stops being
    fresh) and `expires` (the hard-delete point). Between the two an entry is
    *stale* — still returned, but flagged so callers can revalidate in the
    background (stale-while-revalidate). Entries with no stale window have
    `fresh == expires`, so they simply disappear at expiry as before.

    Values must be JSON-serializable. A stored value may be None (negative
    cache). get() returns the freshest usable value or the _MISS sentinel when
    a key is absent or hard-expired; get_swr() additionally reports staleness.
    SQLite work runs in a worker thread so it never blocks the loop. Memory is
    bounded by serialized-text bytes, so a few thousand huge package indexes
    can be cached on disk without ever holding them all in RAM.
    """

    def __init__(self, path: Path, mem_budget: int = MEM_BUDGET):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS cache "
            "(key TEXT PRIMARY KEY, body TEXT, fresh REAL, expires REAL)"
        )
        # Migrate pre-SWR databases: add `fresh` and treat old rows as having
        # no stale window (fresh == their existing expiry).
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(cache)")}
        if "fresh" not in cols:
            self._db.execute("ALTER TABLE cache ADD COLUMN fresh REAL")
            self._db.execute("UPDATE cache SET fresh = expires WHERE fresh IS NULL")
        self._db.commit()
        self._lock = threading.Lock()
        self._mem: OrderedDict[str, tuple[float, float, object, int]] = OrderedDict()
        self._mem_bytes = 0
        self._mem_budget = mem_budget

    # --- in-memory tier (touched only from the event-loop thread) ---
    def _mem_get(self, key: str, now: float):
        """Return (val, is_stale) for a live entry, or _MISS if absent/expired."""
        hit = self._mem.get(key)
        if hit is None:
            return _MISS
        fresh, exp, val, _ = hit
        if exp <= now:
            self._mem_drop(key)
            return _MISS
        self._mem.move_to_end(key)
        return val, fresh <= now

    def _mem_drop(self, key: str):
        old = self._mem.pop(key, None)
        if old:
            self._mem_bytes -= old[3]

    def _mem_put(self, key: str, fresh: float, exp: float, val: object, nbytes: int):
        self._mem_drop(key)
        self._mem[key] = (fresh, exp, val, nbytes)
        self._mem_bytes += nbytes
        while self._mem_bytes > self._mem_budget and self._mem:
            k, item = self._mem.popitem(last=False)
            self._mem_bytes -= item[3]

    # --- sqlite tier (run via asyncio.to_thread) ---
    def _db_get(self, key: str, now: float):
        with self._lock:
            row = self._db.execute(
                "SELECT body, fresh, expires FROM cache WHERE key=?", (key,)
            ).fetchone()
        if row is None or row[2] <= now:
            return _MISS
        return row[0], row[1], row[2]

    def _db_put(self, key: str, body: str, fresh: float, exp: float):
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO cache (key, body, fresh, expires) VALUES (?,?,?,?)",
                (key, body, fresh, exp),
            )
            self._db.commit()

    def _db_purge(self):
        with self._lock:
            self._db.execute("DELETE FROM cache WHERE expires < ?", (time.time(),))
            self._db.commit()

    async def get_swr(self, key: str):
        """Return (val, is_stale) for a usable entry, else _MISS.

        A stale entry is still returned; the caller decides whether to refresh.
        """
        now = time.time()
        res = self._mem_get(key, now)
        if res is not _MISS:
            return res
        row = await asyncio.to_thread(self._db_get, key, now)
        if row is _MISS:
            return _MISS
        body, fresh, exp = row
        val = json.loads(body)
        self._mem_put(key, fresh, exp, val, len(body))
        return val, fresh <= now

    async def get(self, key: str):
        """Fresh-only lookup: returns the value, or _MISS if absent/stale/expired."""
        res = await self.get_swr(key)
        if res is _MISS or res[1]:  # absent or stale
            return _MISS
        return res[0]

    async def set(self, key: str, val: object, ttl: int, stale: int = 0):
        body = json.dumps(val)
        fresh = time.time() + ttl
        exp = fresh + stale
        self._mem_put(key, fresh, exp, val, len(body))
        await asyncio.to_thread(self._db_put, key, body, fresh, exp)

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
# Strong refs to fire-and-forget revalidation tasks, so they aren't GC'd mid-flight.
_background_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global cache, sem
    cache = Cache(CACHE_PATH)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    # PyPI content-negotiates: the /stats/ endpoint returns HTML unless we ask
    # for JSON. The /pypi/.../json endpoints return JSON regardless, so this
    # default is safe for every request.
    app.state.client = httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=20
    )

    async def purge_loop():
        while True:
            await asyncio.sleep(PURGE_INTERVAL)
            await cache.purge()

    purger = asyncio.create_task(purge_loop())
    try:
        yield
    finally:
        purger.cancel()
        for t in _background_tasks:
            t.cancel()
        with suppress(asyncio.CancelledError):
            await purger
            await asyncio.gather(*_background_tasks, return_exceptions=True)
        await app.state.client.aclose()
        cache.close()


app = FastAPI(lifespan=lifespan)


async def _fetch_json(client: httpx.AsyncClient, url: str, ttl: int, stale: int) -> dict | None:
    """Fetch `url`, cache it, and resolve its single-flight future.

    The caller must have already registered `_inflight[url]`. Successful
    responses get a fresh `ttl` plus a `stale` window; failures get a brief
    negative cache with no stale window (no point serving a stale 404).
    """
    fut = _inflight[url]
    try:
        try:
            async with sem:  # cap simultaneous PyPI requests
                r = await client.get(url)
            r.raise_for_status()
            data = r.json()
            await cache.set(url, data, ttl, stale)
        except Exception:
            data = None
            await cache.set(url, None, NEG_TTL)  # brief negative cache
        fut.set_result(data)
        return data
    finally:
        _inflight.pop(url, None)


def _revalidate(client: httpx.AsyncClient, url: str, ttl: int, stale: int) -> None:
    """Kick off a background refresh of `url` unless one is already running."""
    if url in _inflight:
        return
    _inflight[url] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(_fetch_json(client, url, ttl, stale))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def get_json(
    client: httpx.AsyncClient, url: str, ttl: int = CACHE_TTL, stale: int = STALE_TTL
) -> dict | None:
    """GET JSON via the shared cache, single-flight, and concurrency cap.

    On a stale hit the cached value is returned immediately and a background
    revalidation is scheduled (stale-while-revalidate). `ttl`/`stale` let
    immutable endpoints (per-version metadata) live far longer than the
    mutable package index.
    """
    cached = await cache.get_swr(url)
    if cached is not _MISS:
        val, is_stale = cached
        if is_stale:
            _revalidate(client, url, ttl, stale)
        return val
    if url in _inflight:
        return await _inflight[url]

    _inflight[url] = asyncio.get_running_loop().create_future()
    return await _fetch_json(client, url, ttl, stale)


async def get_index(client: httpx.AsyncClient, name: str) -> dict | None:
    """All releases of a package (versions + files), for version selection.

    Mutable — new releases and yanks appear here — so it keeps the short TTL.
    """
    return await get_json(client, f"https://pypi.org/pypi/{name}/json")


async def get_version_meta(client: httpx.AsyncClient, name: str, version: str) -> dict | None:
    """Metadata for one specific version — carries that version's requires_dist.

    Immutable once published, so it lives far longer than the package index.
    """
    return await get_json(
        client,
        f"https://pypi.org/pypi/{name}/{version}/json",
        ttl=VERSION_TTL,
        stale=VERSION_STALE_TTL,
    )


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

    Returns (info, truncated): canonical name -> {name, version, size}, and
    whether expansion stopped early at MAX_PACKAGES.
    """
    root_canon = canonicalize_name(root.name)
    name_for = {root_canon: root.name}
    node_spec: dict[str, SpecifierSet] = {}
    node_extras: dict[str, set[str]] = {}
    processed: dict[str, tuple] = {}        # canon -> (version, frozenset(extras)) last expanded
    info: dict[str, dict] = {}              # canon -> {name, version, size}
    truncated = False
    pending: dict[str, tuple[SpecifierSet, set[str]]] = {
        root_canon: (root.specifier, set(root.extras))
    }

    while pending:
        # Abuse guard: stop growing the graph past a sane ceiling.
        if len(node_spec) > MAX_PACKAGES:
            truncated = True
            break
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

    return info, truncated


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

    try:
        info, truncated = await asyncio.wait_for(
            resolve(request.app.state.client, req), timeout=RESOLVE_TIMEOUT
        )
    except asyncio.TimeoutError:
        return JSONResponse(
            {"error": f"Resolving '{req.name}' timed out"}, status_code=504
        )

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
        "truncated": truncated,
    }
    await cache.set(key, result, CACHE_TTL)
    return result


@app.get("/api/top")
async def api_top(request: Request):
    """Seed the home page with the heaviest projects on PyPI.

    Sourced from PyPI's own /stats/ endpoint (top ~100 projects by total upload
    footprint). We expose only the *names* — pysize's job is to show each one's
    install size and dependency count once clicked, not to restate PyPI's
    storage numbers. `total_size` is PyPI's all-of-PyPI figure, used purely as a
    scale-setting fact on the landing page.
    """
    data = await get_json(request.app.state.client, "https://pypi.org/stats/")
    if not data:
        return JSONResponse({"error": "PyPI stats unavailable"}, status_code=502)
    top = data.get("top_packages") or {}
    names = sorted(top, key=lambda n: top[n].get("size") or 0, reverse=True)
    return {
        "total_size": data.get("total_packages_size") or 0,
        "packages": names[:TOP_PACKAGES_COUNT],
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    return Path(__file__).with_name("index.html").read_text()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8731, log_level="warning")
