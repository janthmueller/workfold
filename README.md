# Workfold

Workfold is a private, local CLI that folds timestamped activity from Git and
filesystem metadata onto one representative Monday-to-Sunday week. It makes the
shape of recorded activity easy to scan and marks events outside a configured
working schedule without pretending to measure hours worked.

```text
Time          Mon       Tue       Wed       Thu       Fri       Sat       Sun
07:48–08:12  ○●■       ●
              ⋮ 1h 8m
09:20–09:29                      ■●
```

Terminal output starts directly with this matrix, followed by a content-aware
symbol key and three direct statistic rows: total events, the inside/outside
schedule split, and the weekday/weekend calendar split. There is no summary
heading. The key has no heading and names only categories actually
visible in the matrix; its `×N` explanation appears only when a cell uses exact
count notation. The configured working hours follow on their own left-aligned
line. Schedule and calendar percentages each use the total event count as their
denominator; weekend activity may also be outside the configured schedule. A
normal complete run stops there.
`--verbose` adds scope, period, the full successful coverage status, operational
metadata, and the detailed coverage ledger. Use `--coverage` when the ledger is
wanted without those verbose details. Partial collection, explicit narrowing,
unsupported capabilities, and nonzero display cropping remain visible by
default as exception notices; compact output never hides those limitations.

Each ordinary symbol is one activity marker. Circles are Git and squares are
filesystem evidence; filled shapes are inside working hours and hollow red
shapes are outside. Git-inside markers are green and filesystem-inside markers
are blue. Shapes preserve the same meaning with `--no-color`. Rows are greedy,
globally aligned time clusters (one hour by default), so empty time disappears
without losing the order expressed by the labels.

## Status

Workfold is an alpha, terminal-only CLI with its sparse terminal renderer and
local collection pipeline implemented end to end. Git commits, per-file
changes, annotated tags, local
reflogs, filesystem metadata, combined-source views, the `full` profile, strict
coverage accounting, and outside-event listing share the same normalized
pipeline.

See the [MVP specification](https://github.com/janthmueller/workfold/blob/main/MVP.md)
for exact semantics and the
[implementation plan](https://github.com/janthmueller/workfold/blob/main/PLAN.md)
for delivery status. The Astro site in `docs/` documents the terminal product;
it is not an HTML reporting feature.

## Install

Workfold requires Python 3.10 or newer. Git collection uses the installed
`git` executable and never contacts a remote.

Install Workfold from PyPI:

```bash
pip install workfold
workfold --help
```

For an isolated CLI environment, `uv tool install workfold` and
`pipx install workfold` are supported alternatives. Standalone archives for
supported platforms are attached to
[GitHub releases](https://github.com/janthmueller/workfold/releases).

## Quick Git view

Run inside a repository to fold the current ISO week's author timestamps from
commits reachable through all local refs:

```bash
workfold
```

This uses the default selectors `-t this-week -m git -p standard`.
`-t/--time` also accepts an ISO week, repeated ISO weeks, an inclusive
`DATE..DATE` range with either endpoint open, or `all`. `-m/--mode` accepts
`git`, `fs`, or `all`; changing time never changes mode and vice versa.

Common selections include:

```bash
workfold . -t 2026-W31
workfold . -t 2026-W30 -t 2026-W31
workfold . -t 2026-07-01..2026-07-31
workfold . -t 2026-01-01..
workfold . -t all -m git --git-commit-times author,committer --coverage
workfold . --hours 'Mo-Thu 08:00-16:30; Fr 08:00-14:00'
workfold . --cluster-window 30s
workfold . --cluster-window 10m
workfold . --cluster-window 1h5m
workfold . --cluster-window '1h 5m'
workfold . --timezone Europe/Berlin --list-outside --limit 50
```

Chart band labels always use `HH:MM`, even for second-level cluster windows and
events. Exact seconds and nanoseconds remain in normalized provenance and the
outside-hours event list.

For portable Git-object evidence across all available history, use:

```bash
workfold . -t all -m git -p portable
```

The portable profile includes commit author and committer timestamps plus
annotated-tag tagger timestamps. It does not use filesystem metadata or local
reflogs, contact a remote, or claim to show when an object was published
remotely.

The exhaustive local command is:

```bash
workfold . -t all -m all -p full
```

`-p/--profile` defaults to `standard`. The `full` profile broadens timestamp
and record collection only inside the selected time and mode. It never changes
either selector. With `-m all`, it requests all
supported Git and filesystem evidence, ignored entries, and all local refs. It
changes collection scope only: detailed ledger output still requires
`--coverage` or `--verbose`. It does not parse date-like text inside files.

## Accuracy and privacy

A Workfold event is a discrete timestamp observation, not a work session. Git
file changes are derived from first-parent tree differences, not stored human
actions. Commits can be rewritten; annotated tags have independent tagger dates
while lightweight tags do not; and reflogs can expire. Filesystem metadata is a
mutable current snapshot: Linux birth time is collected through `statx` when
the filesystem returns it, birth time remains platform-dependent elsewhere,
ctime is metadata-change time, and atime may be unreliable. Workfold cannot
recover unsaved edit sessions, deleted untracked files, or earlier metadata
values.

Collection remains local. No Git hosting API, telemetry service, web server, or
network collector is part of the MVP. Coverage reports account for captured,
filtered, unavailable, unsupported, and unreadable timestamps within the exact
requested scope instead of claiming unqualified completeness.

## Development

The project intentionally uses a flat Python package: the implementation is in
`workfold/`, with no `src/` wrapper.

```bash
nix develop
uv sync --extra dev
uv run pytest
ruff check .
ruff format --check .
uv run pyright
uv run workfold --help
```

Build the Python distributions and validate the Astro documentation with:

```bash
uv build
nix run .#docs-check
nix run .#docs-build
```

Start the documentation site locally with `nix run .#docs-dev`.
