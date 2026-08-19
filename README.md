# Wuf

[![PyPI Latest Release](https://img.shields.io/pypi/v/wuf.svg)](https://pypi.org/project/wuf/)
[![Pepy Total Downloads](https://img.shields.io/pepy/dt/wuf)](https://pepy.tech/project/wuf)
[![GitHub License](https://img.shields.io/github/license/janthmueller/wuf)](https://github.com/janthmueller/wuf/blob/main/LICENSE)

**WUF Unifies Footprints.**

Wuf folds local Git and filesystem timestamp footprints onto one representative
Monday-to-Sunday week. It highlights activity outside your intended working
hours without pretending that timestamped events are hours worked.

![Wuf terminal output](https://raw.githubusercontent.com/janthmueller/wuf/main/docs/public/wuf-output.svg)

Wuf is local, private by design, terminal-only, and currently alpha.

## Install

```bash
pip install wuf
wuf --help
```

For an isolated installation, use `uv tool install wuf` or
`pipx install wuf`. Standalone binaries are available from
[GitHub releases](https://github.com/janthmueller/wuf/releases).

### Renamed from Workfold

Wuf is the continuation of Workfold under its shorter, permanent name. Wuf
packages also install `workfold` as a compatibility command, but `wuf` is the
canonical command going forward. Existing configuration should be renamed from
`workfold.toml` to `wuf.toml`, and `[tool.workfold]` tables should become
`[tool.wuf]`.

## Quick start

Run `wuf` inside a Git repository to see the current ISO week:

```bash
wuf
```

The selectors you will use most are:

```bash
wuf . -t 2w3d                    # rolling elapsed window
wuf . -t 2026-W31                # one ISO week
wuf . -p fs                      # current filesystem metadata
wuf . -p both                    # low-noise Git + filesystem view
wuf . -p portable -t all         # evidence stored in Git objects
wuf . -p full -t all --git-commits-from all-refs --include-ignored
wuf . -e git:tag:tagger fs:file:modified
```

They control separate parts of the request:

| Selector | Purpose | Values |
| --- | --- | --- |
| `-t`, `--time` | Date scope | `this-week`, `2w3d`, `YYYY-Www`, `DATE..DATE`, `all` |
| `-p`, `--profile` | Named event set | `git`, `fs`, `both`, `portable`, `full` |
| `-e`, `--events` | Exact event set (alternative to a profile) | IDs and wildcards |

The profiles answer different questions:

- `git`: What does ordinary commit activity look like? This is the default.
- `fs`: What birth and modification metadata exists for current regular files?
- `both`: What does the combined low-noise Git and filesystem pattern look like?
- `portable`: What dated evidence is stored inside Git objects?
- `full`: Which timestamps exist across every supported Git and filesystem event kind?

Profiles expand only to event sets. Time, Git reachability, ignored files, and
explicit exclusions remain independently configurable.

For exact control, `-e/--events` accepts identifiers such as
`git:commit:author`, `git:tag:tagger`, and `fs:file:modified`; quote wildcards
such as `'git:*'`. `-l/--list` appends bounded event details. Paths must appear
before either space-separated selector, or after an option-terminating `--`.

Schedules support daily intervals, breaks, overnight shifts, and `all`:

```bash
wuf . --hours 'Mo-Thu 08:00-16:30; Fr 08:00-14:00'
wuf . --hours 'Mo-Fr 22:00-06:00'
wuf . --hours all
```

See the [usage guide](https://janthmueller.github.io/wuf/guides/usage/)
for clustering, fixed bands, identity markers, day hiding, grids, exact event
selection, configuration, and every CLI option.

## Configuration

Put personal defaults in the platform configuration directory, or project
defaults in `wuf.toml`:

```toml
timezone = "Europe/Berlin"
hours = "Mo-Thu 08:00-16:30; Fr 08:00-14:00"
profile = "portable"
cluster-anchor = "midnight"
band-label = "start"
show-empty-bands = true
count-grouping = "visual"
grid = "vertical"
hide-empty-days = ["weekend"]

[styles."git:tag:*"]
symbol = "◆"
color = "magenta"
outside-symbol = "◇"
outside-color = "bright_red"
```

Python projects may use `[tool.wuf]` in `pyproject.toml` instead. Values
resolve as built-in → global → nearest project → CLI. Inspect the result and
each value's origin without collecting timestamps:

```bash
wuf . --show-config
```

Use `--config FILE` for one exact file or `--no-config` for built-ins plus CLI
only. The [usage guide](https://janthmueller.github.io/wuf/guides/usage/#configuration-files)
documents locations, discovery, merging, and every supported key.

## Reading the chart

- By default, circles are Git events and squares are filesystem events.
- Default green/blue filled markers are inside the schedule; red hollow markers
  are outside. Colorless output preserves the shape distinction, and event
  style rules may replace both symbol pairs and their colors.
- One symbol is one event; busy cells use exact `×N` counts. Counts stay
  separate per event kind by default; `--count-grouping visual` merges kinds
  only when their resolved symbol and configured color match.
- Empty time is compressed, and `⋮` reports a meaningful gap.
- Identity-marker mode replaces Git circles with codes mapped in the key.
- Event-selector style rules can replace source-marker symbols and colors while
  keeping collection and coverage unchanged.

The summary independently splits all events by schedule and by calendar day.
Weekend events can therefore also be outside working hours.

## Accuracy and privacy

- Events are discrete timestamp observations, not work sessions or duration.
- Collection is local: Wuf does not contact a Git host or telemetry service.
- Git history can be rewritten; reflogs can expire; filesystem metadata is a
  mutable snapshot and birth time depends on platform and filesystem support.
- Coverage reports unavailable, unsupported, and unreadable evidence that can
  prevent a complete answer. Known timestamps outside the requested time or
  identity scope are not counted as coverage outcomes.

See the [documentation](https://janthmueller.github.io/wuf/) for every CLI
option, collector semantics, coverage guarantees, and platform notes.

## Development

The Python package lives directly in `wuf/`; there is no `src/` wrapper.

```bash
nix develop
uv sync --locked --extra dev
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
```

Use `nix run .#docs-dev` for the documentation site and
`nix run .#docs-check` to validate it. The
[architecture guide](https://janthmueller.github.io/wuf/reference/architecture/)
documents the package boundaries, dependency rules, data pipeline, and test
layout.
