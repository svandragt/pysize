# /// script
# requires-python = ">=3.10"
# dependencies = ["fastapi", "uvicorn", "httpx", "packaging"]
# ///
"""Pysize — a bundlephobia-style size explorer for PyPI packages."""

import asyncio
import time
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

app = FastAPI()

CACHE_TTL = 3600  # seconds to keep a PyPI response

# Shared across requests: URL -> (expiry_monotonic, parsed_json_or_None).
_cache: dict[str, tuple[float, dict | None]] = {}
# In-flight de-duplication: URL -> Future, so concurrent callers share one fetch.
_inflight: dict[str, asyncio.Future] = {}
# Resolved-result cache: requirement key -> (expiry_monotonic, response dict).
_resolve_cache: dict[str, tuple[float, dict]] = {}


async def get_json(client: httpx.AsyncClient, url: str) -> dict | None:
    """GET JSON with a shared TTL cache and single-flight de-duplication."""
    now = time.monotonic()
    hit = _cache.get(url)
    if hit and hit[0] > now:
        return hit[1]
    if url in _inflight:
        return await _inflight[url]

    fut = asyncio.get_running_loop().create_future()
    _inflight[url] = fut
    try:
        try:
            r = await client.get(url, timeout=20)
            r.raise_for_status()
            data = r.json()
            _cache[url] = (now + CACHE_TTL, data)
        except Exception:
            data = None
            _cache[url] = (now + 60, None)  # brief negative cache
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
    env = default_environment()
    # Evaluate against each requested extra plus the base (no-extra) case;
    # include if any matches. Markers that don't reference `extra` evaluate
    # the same regardless, so the base case covers ordinary platform markers.
    for e in {""} | set(extras):
        if req.marker.evaluate({**env, "extra": e}):
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
async def api_size(pkg: str):
    try:
        req = Requirement(pkg.strip())
    except Exception:
        return JSONResponse({"error": f"Could not parse '{pkg}'"}, status_code=400)

    # Cache the whole resolution, not just the upstream HTTP responses, so a
    # repeated query skips the graph walk entirely.
    key = f"{canonicalize_name(req.name)}[{','.join(sorted(req.extras))}]{req.specifier}"
    now = time.monotonic()
    hit = _resolve_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]

    async with httpx.AsyncClient(headers={"User-Agent": "pysize-poc"}) as client:
        info = await resolve(client, req)

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
    _resolve_cache[key] = (now + CACHE_TTL, result)
    return result


@app.get("/", response_class=HTMLResponse)
async def index():
    return Path(__file__).with_name("index.html").read_text()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8731, log_level="warning")
