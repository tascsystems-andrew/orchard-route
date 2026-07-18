# Contributing

Contributions are welcome — bug reports, benchmarks on different Apple Silicon
tiers, and code.

## The licensing grant (read before your first PR)

Orchard Route is dual-licensed (AGPL-3.0-only + commercial; see
[README](README.md#license)). For that model to work, the project must be able to
relicense contributed code under the commercial terms.

By submitting a contribution (pull request, patch, or code in an issue) you agree
that:

1. Your contribution is licensed to the project under **AGPL-3.0-only**, and
2. You grant **Andrew Sanche** a perpetual, worldwide, irrevocable right to
   relicense your contribution under other terms, including commercial licenses,
   and
3. The contribution is your own work and you have the right to grant the above.

This is the standard trade of every dual-licensed project (Qt, MySQL, MongoDB
before SSPL): the copyleft protects the commons, the grant keeps the project
fundable. If you're not comfortable with it, opening an issue describing your
change instead of a PR is genuinely appreciated — ideas don't need a grant.

## Practical notes

- Run the test suite before a PR: every `test_*.py` at repo root, with
  `.venv/bin/python`. GPU tests need an Apple Silicon Mac.
- The kernels are validated against CPU ground truth (Dijkstra) — changes to
  `wavefront.py` / `batch_sssp.py` must keep those exact-agreement tests passing.
- Style: module docstrings explain design intent; comments state constraints the
  code can't show. Match what's there.
