# Workfold
[![PyPI Latest Release](https://img.shields.io/pypi/v/workfold.svg)](https://pypi.org/project/workfold/)
[![Pepy Total Downloads](https://img.shields.io/pepy/dt/workfold)](https://pepy.tech/project/workfold)
[![GitHub License](https://img.shields.io/github/license/janthmueller/workfold)](https://github.com/janthmueller/workfold/blob/main/LICENSE)

Workfold folds local Git and filesystem timestamps onto one representative
Monday-to-Sunday week. It highlights activity outside your intended working
hours without pretending that timestamped events are hours worked.

![Workfold terminal output](https://raw.githubusercontent.com/janthmueller/workfold/main/docs/public/workfold-output.svg)

Workfold is local, private by design, terminal-only, and currently alpha.

## Install

```bash
pip install workfold
workfold --help
```

For an isolated installation, use `uv tool install workfold` or
`pipx install workfold`. Standalone binaries are available from
[GitHub releases](https://github.com/janthmueller/workfold/releases).

## Quick start

Run `workfold` inside a Git repository for the current ISO week:

```bash
workfold
```

Common views:

```bash
workfold . -t 2026-W31                         # one ISO week
workfold . -t 2026-W30 -t 2026-W31            # several weeks, folded together
workfold . -t 2026-07-01..2026-07-31          # inclusive date range
workfold . -t all -m fs                        # filesystem metadata
workfold . -t all -m git -p portable           # portable Git-object timestamps
workfold . -t all -m all -p full               # exhaustive local view
workfold . --git-identity jan@example.com      # only that recorded Git identity
workfold . --timezone Europe/Berlin
workfold . --hours 'Mo-Thu 08:00-16:30; Fr 08:00-14:00'
workfold . --list-outside --limit 50
```

The three main selectors are independent:

| Selector | Purpose | Values |
| --- | --- | --- |
| `-t`, `--time` | Date scope | `this-week`, `YYYY-Www`, `DATE..DATE`, `all` |
| `-m`, `--mode` | Evidence source | `git`, `fs`, `all` |
| `-p`, `--profile` | Collection depth | `standard`, `portable`, `full` |

- `standard` — **What does the ordinary activity pattern look like?** Git uses
  commit author dates reachable from local branches (plus a detached `HEAD`);
  filesystem mode uses birth/modified dates for regular files and respects Git
  ignore rules.
- `portable` — **What dated evidence is stored inside Git objects?** Includes
  commit author/committer and annotated-tag tagger dates, excluding local-only
  evidence.
- `full` — **What dated evidence can this local machine still discover?**
  Enables every supported kind inside the selected time and mode; it does not
  imply `-t all` or `-m all`.

Use `--cluster-window 10m`, `--cluster-window 1h5m`, or another duration to tune
row clustering. Use `--no-color` or the standard `NO_COLOR` environment
variable for colorless output.

## Reading the chart

- Circles are Git events; squares are filesystem events.
- Green and blue are inside the configured schedule; red is outside.
- One symbol is one event. Busy cells use exact `×N` counts.
- Empty time is omitted. A `⋮` row reports a compressed gap.

The summary independently splits all events by schedule and by calendar day.
Weekend events can therefore also be outside working hours.

## Accuracy and privacy

- Events are discrete timestamp observations, not work sessions or duration.
- Collection is local: Workfold does not contact a Git host or telemetry service.
- Git history can be rewritten; reflogs can expire; filesystem metadata is a
  mutable snapshot and birth time depends on platform and filesystem support.
- Coverage output accounts for unavailable, filtered, unsupported, and
  unreadable timestamps in the requested scope.

See the [documentation](https://janthmueller.github.io/workfold/) for every CLI
option, collector semantics, coverage guarantees, and platform notes.

## Development

The Python package lives directly in `workfold/`; there is no `src/` wrapper.

```bash
nix develop
uv sync --extra dev
uv run pytest
ruff check .
ruff format --check .
uv run pyright
```

Use `nix run .#docs-dev` for the documentation site and
`nix run .#docs-check` to validate it.
