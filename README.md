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
workfold . -t 2w3d                             # rolling elapsed window
workfold . -t 2026-07-01..2026-07-31          # inclusive date range
workfold . -t all -m fs                        # filesystem metadata
workfold . -t all -m git -p portable           # portable Git-object timestamps
workfold . -t all -m both -p full              # exhaustive local view
workfold . --git-identity jan@example.com      # only that recorded Git identity
workfold . --marker-style identity             # identity codes instead of circles
workfold . --timezone Europe/Berlin
workfold . --hours 'Mo-Thu 08:00-16:30; Fr 08:00-14:00'
workfold . --hours all                         # classify every time as working time
workfold . --cluster-anchor midnight           # fixed clock-aligned intervals
workfold . --cluster-anchor midnight --show-empty-bands  # include empty fixed intervals
workfold . --band-label start                  # show one time per occupied row
workfold . -E all                              # keep only occupied day columns
workfold . -E weekend                          # remove empty weekend columns
workfold . -H weekend                          # always hide weekend columns
workfold . --grid vertical                     # add column separators
workfold . --list-outside --limit 50
```

The three main selectors control separate parts of the request:

| Selector | Purpose | Values |
| --- | --- | --- |
| `-t`, `--time` | Date scope | `this-week`, `2w3d`, `YYYY-Www`, `DATE..DATE`, `all` |
| `-m`, `--mode` | Evidence source | `git`, `fs`, `both` |
| `-p`, `--profile` | Evidence preset | `standard`, `portable`, `full` |

Time and mode independently choose when and where to collect. A profile chooses
which evidence kinds to use inside that request and never changes time or mode;
the `portable` preset is intentionally available only with Git mode.

- `standard` — **What does the ordinary activity pattern look like?** Git uses
  commit author dates reachable from local branches (plus a detached `HEAD`);
  filesystem mode uses birth/modified dates for regular files and respects Git
  ignore rules.
- `portable` — **What dated evidence is stored inside Git objects?** Includes
  commit author/committer and annotated-tag tagger dates, excluding local-only
  evidence.
- `full` — **What dated evidence can this local machine still discover?**
  Enables every supported kind inside the selected time and mode; it does not
  imply `-t all` or `-m both`.

Use `--cluster-window 10m`, `--cluster-window 1h5m`, or another duration to tune
row clustering. The default `--cluster-anchor event` starts a band at each
earliest unassigned event; `midnight` uses fixed whole-minute intervals from
local `00:00` so their `HH:MM` boundaries remain exact. Second-based windows
remain available with event anchoring. Independently,
`--band-label range|start` selects an observed/fixed range label or only its
starting minute. `--show-empty-bands` with midnight anchoring renders every
fixed interval intersecting the display range; automatic ranges expand to full
fixed bands. An explicitly cropped partial edge always shows its exact range,
even with `--band-label start`. Without dense output, empty time stays compressed.
Color is automatic by default. Use `--no-color` or the standard `NO_COLOR`
environment variable for colorless output. `--color` restores automatic color
when a configuration file sets `no-color = true`.

## Configuration

Put personal defaults in the platform configuration directory, or project
defaults in `workfold.toml`:

```toml
timezone = "Europe/Berlin"
hours = "Mo-Thu 08:00-16:30; Fr 08:00-14:00"
mode = "git"
profile = "portable"
cluster-anchor = "midnight"
band-label = "start"
show-empty-bands = true
grid = "vertical"
hide-empty-days = ["weekend"]
```

Python projects may use `[tool.workfold]` in `pyproject.toml` instead. Values
resolve as built-in → global → nearest project → CLI. Inspect the result and
each value's origin without collecting timestamps:

```bash
workfold . --show-config
```

Use `--config FILE` for one exact file or `--no-config` for built-ins plus CLI
only. The [usage guide](https://janthmueller.github.io/workfold/guides/usage/#configuration-files)
documents locations, discovery, merging, and every supported key.

## Reading the chart

- Circles are Git events; squares are filesystem events.
- `--marker-style identity` replaces Git circles with mapped codes such as `J`
  or the collision-safe `J1`, `J2`, and `J3`.
- Green and blue are inside the configured schedule; red is outside.
- Filled/uppercase markers are inside; hollow/lowercase markers are outside.
- The key maps each visible identity/source once and adds an outside-hours cue
  only when needed.
- One symbol is one event. Busy cells use exact `×N` counts.
- Empty time is omitted. A `⋮` row reports a compressed gap once it reaches
  the configured cluster window.
- Event-anchored rows are compact around observations; midnight-anchored rows
  use predictable clock boundaries. Labels are independently configurable.
- Day-column hiding changes only the matrix; totals continue to cover every
  selected event.
- `--grid vertical|horizontal|both` adds optional internal chart lines; the
  uncluttered default is `none`.

The summary independently splits all events by schedule and by calendar day.
Weekend events can therefore also be outside working hours.

## Accuracy and privacy

- Events are discrete timestamp observations, not work sessions or duration.
- Collection is local: Workfold does not contact a Git host or telemetry service.
- Git history can be rewritten; reflogs can expire; filesystem metadata is a
  mutable snapshot and birth time depends on platform and filesystem support.
- Coverage reports unavailable, unsupported, and unreadable evidence that can
  prevent a complete answer. Known timestamps outside the requested time or
  identity scope are not counted as coverage outcomes.

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
