# pysize

A [bundlephobia](https://bundlephobia.com)-style size explorer for **PyPI**
packages. Type a package — `requests`, or `headroom-ai[all]` with extras — and
see its install size: the wheel plus every dependency, deduplicated, sorted
largest-first. Click any dependency to drill into it; the URL is shareable.

It resolves entirely against the public PyPI JSON API — no installing, no
sandbox.

## Quick start

```bash
make run        # or: uv run main.py
# open http://127.0.0.1:8731
```

That's it — [uv](https://docs.astral.sh/uv/) reads the dependencies from the
inline script header in `main.py` and runs the server. Run `make help` for the
other targets (`verify`, `ping`, `deploy`, `logs`, `clean`).

## How sizes are measured

The total is the **download size of one wheel per package** (prefers the
`py3-none-any` wheel), summed across the package and the full deduplicated
dependency set. It is a close analog to bundlephobia's number, not an exact
on-disk install size — see [docs/accuracy.md](docs/accuracy.md).

## API

```
GET /api/size?pkg=<requirement>
```

`<requirement>` is any PEP 508 requirement: `flask`, `httpx`, `boto3`,
`headroom-ai[all]`, `django>=5`. Response:

```jsonc
{
  "name": "Flask",
  "version": "3.x",
  "self_size": 612345,        // bytes, the package's own wheel
  "total_size": 618000,       // bytes, deduplicated total
  "dep_count": 6,             // unique dependencies
  "packages": [               // each unique dependency, largest first
    { "name": "Werkzeug", "version": "3.x", "size": 234567 },
    ...
  ]
}
```

## Documentation

- [How it works](docs/how-it-works.md) — the resolver and the two cache layers.
- [Accuracy](docs/accuracy.md) — how results are verified against `uv`, and the
  known gaps.
- [Deployment](ansible/README.md) — Ansible playbook to run it behind nginx as a
  systemd service.

## Verifying correctness

`verify.py` resolves random popular packages and diffs the dependency set
against a real resolver (`uv pip compile`):

```bash
uv run verify.py 5          # 5 random popular packages
uv run verify.py flask boto3 # specific ones
```

## Project layout

| Path | What |
|------|------|
| `main.py` | FastAPI app: resolver, caching, API, serves the page |
| `index.html` | Single-page frontend |
| `verify.py` | Correctness check against `uv` |
| `ansible/` | Deployment playbook (nginx + systemd) |
| `Makefile` | Common tasks (`make help`) |
| `docs/` | Documentation |

## Status

A proof of concept. It resolves the latest version satisfying each specifier
rather than producing a fully locked resolution, and reports wheel download
size. Good enough to compare packages at a glance; not a substitute for actually
installing.
