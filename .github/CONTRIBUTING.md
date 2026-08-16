# Contributing to Workfold

Thanks for helping improve Workfold. Start with the
[development guide](https://janthmueller.github.io/workfold/guides/development/),
which documents the architecture, collector invariants, tests, and release
flow.

Before opening a pull request, run:

```bash
uv sync --locked --extra dev
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
```

Keep collection, normalized evidence, application policy, and terminal
rendering separate. New collectors must preserve provenance and reconcile every
requested timestamp slot as captured, filtered, unavailable, unsupported, or
errored. Do not include unrelated generated files or user-specific data.

Use a focused conventional commit when practical. By contributing, you agree
that your contribution is licensed under the repository's MIT license.
