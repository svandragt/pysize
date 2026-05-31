# /// script
# requires-python = ">=3.10"
# dependencies = ["packaging"]
# ///
"""Repeatable correctness check for pysize.

Picks N random packages from a list of popular PyPI projects, resolves each
with the running pysize server, and compares the dependency set against
`uv pip compile`. Run with the server up on :8731.

    uv run verify.py            # 5 random popular packages
    uv run verify.py 8          # 8 of them
    uv run verify.py flask boto3 # specific packages
"""

import json
import random
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

from packaging.utils import canonicalize_name

API = "http://127.0.0.1:8731/api/size?pkg="
TARGET_PYTHON = "3.12"  # must match TARGET_PYTHON in main.py for a fair comparison

# A spread of widely-downloaded PyPI packages across domains.
POPULAR = [
    "requests", "urllib3", "boto3", "botocore", "setuptools", "certifi",
    "idna", "charset-normalizer", "python-dateutil", "six", "pyyaml",
    "numpy", "pandas", "scipy", "matplotlib", "scikit-learn", "pillow",
    "flask", "django", "fastapi", "starlette", "uvicorn", "httpx", "aiohttp",
    "sqlalchemy", "pydantic", "click", "rich", "typer", "jinja2", "werkzeug",
    "pytest", "tox", "black", "ruff", "mypy", "isort", "coverage",
    "redis", "celery", "kombu", "psycopg2-binary", "pymongo", "elasticsearch",
    "beautifulsoup4", "lxml", "scrapy", "selenium", "openpyxl", "tqdm",
    "transformers", "torch", "tensorflow", "openai", "anthropic", "langchain",
    "google-api-python-client", "grpcio", "protobuf", "cryptography", "paramiko",
]


def mine(pkg: str) -> dict | None:
    try:
        with urllib.request.urlopen(API + urllib.parse.quote(pkg), timeout=120) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}


def uv_set(pkg: str) -> set[str] | None:
    with tempfile.NamedTemporaryFile("w", suffix=".in", delete=True) as f:
        f.write(pkg + "\n")
        f.flush()
        r = subprocess.run(
            ["uv", "pip", "compile", f.name, "--python-version", TARGET_PYTHON, "-q"],
            capture_output=True, text=True,
        )
    if r.returncode != 0:
        return None
    out = set()
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(canonicalize_name(line.split("==")[0].split(" ")[0]))
    return out


def main() -> None:
    args = sys.argv[1:]
    if args and not args[0].isdigit():
        pkgs = args
    else:
        n = int(args[0]) if args else 5
        pkgs = random.sample(POPULAR, min(n, len(POPULAR)))

    print(f"comparing {len(pkgs)} package(s) vs `uv` (target py{TARGET_PYTHON})\n")
    print(f"{'package':16} {'total':>11}  {'mine':>4} {'uv':>4}  match  diff")
    print("-" * 60)
    for pkg in pkgs:
        d = mine(pkg)
        if d is None or "error" in d:
            print(f"{pkg:16} {'ERROR':>11}  {d.get('error', '?')[:30] if d else '?'}")
            continue
        my_set = {canonicalize_name(d["name"])} | {
            canonicalize_name(p["name"]) for p in d["packages"]
        }
        uv = uv_set(pkg)
        if uv is None:
            print(f"{pkg:16} {d['total_size']/1048576:8.2f} MB  uv compile failed")
            continue
        missing = uv - my_set   # uv has, we don't
        extra = my_set - uv     # we have, uv doesn't
        ok = "OK" if not missing and not extra else ""
        diff = ""
        if missing:
            diff += "-" + ",".join(sorted(missing))
        if extra:
            diff += (" " if diff else "") + "+" + ",".join(sorted(extra))
        print(f"{pkg:16} {d['total_size']/1048576:8.2f} MB  "
              f"{len(my_set):>4} {len(uv):>4}  {ok:5}  {diff}")


if __name__ == "__main__":
    main()
