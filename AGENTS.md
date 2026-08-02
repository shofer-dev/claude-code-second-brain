# AGENTS.md

Guidance for agents working in this repository — the **Second Brain** Claude
Code plugin (a background observer that drops rare, one-way advisories into a
session). The sibling `CLAUDE.md` is a symlink to this file.

## Read first

- [`README.md`](README.md) — what it is and how it works; [`DESIGN.md`](DESIGN.md)
  — the reasoning, including what was evaluated and rejected;
  [`PRIVACY.md`](PRIVACY.md) — exactly what leaves the machine;
  [`TODO.md`](TODO.md) — known gaps. Keep all four in sync with the code **in
  the same change**, current-state only.

## Invariants

- **Silence is the success metric.** Most passes must produce nothing. Any
  change that makes the advisor speak more often needs an explicit,
  design-level justification in `DESIGN.md` — a chatty Second Brain is a broken
  one.
- **One-way and non-blocking.** The plugin only reads the agent's emissions
  (never tool results) and only ever injects a short advisory. It must never
  gain the ability to block, veto, pause, edit, ask, or write to the
  repository — that boundary is the product.
- **Privacy contract is load-bearing.** Anything that changes what data is
  read, sent, or stored must update `PRIVACY.md` in the same change.

## Working here

- This is a **public FOSS repo** (github.com/shofer-dev). Never commit a
  credential, a private hostname, or a file referencing either by value; the
  plugin takes configuration at runtime.
- Run the tests with [`run-tests.sh`](run-tests.sh) and keep them green before
  committing.
- Commands live in [`commands/`](commands/) (`/second-brain-*`), the observer
  loop in [`worker/`](worker/), hook wiring in [`hooks/`](hooks/), monitors in
  [`monitors/`](monitors/) — skim the directory's files before changing its
  behaviour.
