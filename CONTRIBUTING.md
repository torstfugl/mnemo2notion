# Contributing

Thanks for helping people move their notes freely. A few things keep this codebase safe to
change:

## The format is pinned by tests

`tests/test_mnemo.py` and `tests/test_package.py` restate, assertion by assertion, what
Mnemo's `BlockJsonConverter` (C#) and `wire.ts` (TypeScript) actually read and write. If you
change what this tool emits, change those tests **only** after checking the Mnemo source —
they exist so a format drift fails here instead of corrupting someone's import.

Run everything with:

```bash
python -m unittest discover -s tests -t .
```

The suite needs no network and no token; every API interaction is faked. Keep it that way —
a test that needs a real Notion workspace is a test nobody runs.

## Where things go

- A new **Notion block type** → handler in `convert.py`, test in `test_convert.py`.
- A new **Mnemo block type** → constructor in `mnemo.py`, handler in `reverse.py`,
  tests in both directions. The C# reader throws on unknown payload kinds, so never emit a
  payload except through a `mnemo.py` constructor.
- **Colour changes** → `colors.py` (forward) and `reverse.py` (backward). Check both
  directions still converge: Notion → Mnemo → Notion must be idempotent.
- **API behaviour** (rate limits, retries, caching) → `notion.py`. Writes are never cached;
  see the comment there before touching it.
- **UI** → `notion2mnemo/gui/web/` is plain HTML/CSS/JS with no build step, on purpose:
  contributing to the UI must not require a Node toolchain. All conversion logic stays in
  Python; the page only renders and relays.

## Principles

1. **Nothing is dropped silently.** A block we cannot carry becomes a link, a placeholder,
   or at minimum a warning naming what was lost. This is the tool's whole contract.
2. **Conversions are deterministic.** Ids derive from source ids (`mnemo.stable_id`), never
   from `random`/`uuid4` in conversion paths — re-runs must produce identical output so
   re-imports update instead of duplicating.
3. **Degrade, don't die.** One malformed block costs that block, not the export. Catch at
   the item level, warn, continue.

## Before opening a PR

- `python -m unittest discover -s tests -t .` passes.
- New behaviour has a test that fails without the change.
- User-visible changes are reflected in README.md.

By contributing you agree your contributions are licensed under Apache-2.0.
