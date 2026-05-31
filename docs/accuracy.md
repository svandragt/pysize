# Accuracy

## What "size" means here

The number is the **download size of one wheel per package** (preferring
`py3-none-any`), summed across the package and its full deduplicated dependency
set. That is deliberately the same kind of number bundlephobia reports — a
comparable proxy, not a guarantee.

It is **not**:

- **Installed-on-disk size** — wheels are compressed; unpacked size is larger.
- **Platform-exact** — for packages with compiled wheels (`pydantic-core`,
  `tokenizers`, the `nvidia-cuda-*` set) the `py3-none-any` preference may differ
  from the platform wheel a real install would download.

## How it's verified

`verify.py` resolves packages with pysize and diffs the resulting dependency
**set** against a real resolver, `uv pip compile`, for the same target Python
(3.12):

```bash
uv run verify.py 20    # 20 random popular packages
```

Across a 20-package sample — including large graphs like `scrapy` (34 deps),
`transformers` (27), `openai` (16), `selenium` (15) — the resolved set matches
`uv` **exactly**. Small single-package projects (`numpy`, `redis`, `protobuf`)
and the extras case (`headroom-ai[all]`, ~174 packages) match as well.

The script compares set membership; mismatches are printed as `-missing`
(uv has it, we don't) / `+extra` (we have it, uv doesn't).

## Known gaps

- **No locked resolution.** pysize picks the **highest version satisfying each
  specifier** independently as it walks the graph. A real resolver backtracks to
  find one globally-consistent set. In practice the sets agree for the common
  case, but a project with conflicting constraints can drift by a package or two.
- **Unsatisfiable specifiers fall back.** If a combined specifier matches no
  released version, `best_version()` falls back to the highest available version
  rather than erroring — a POC compromise.
- **Marker target is fixed at 3.12.** Change `TARGET_PYTHON` in `main.py` to
  resolve for a different Python; results for `python_version`-gated and some
  platform-gated dependencies will shift accordingly.

## Reproducing a comparison

```bash
# what pysize resolves
curl -s 'http://127.0.0.1:8731/api/size?pkg=boto3' | python3 -m json.tool

# what a real resolver resolves
printf 'boto3\n' | uv pip compile - --python-version 3.12 -q
```
