# Workfold CLI MVP implementation plan

Status: terminal MVP implemented; hardening and first-release validation remain

Implementation snapshot (2026-08-09): the flat `workfold/` package now contains
the shared observation/coverage model, Git and filesystem collectors, combined
and exhaustive orchestration, the sparse event-symbol terminal renderer, and
end-to-end tests. Release automation and Astro documentation are present. The
unchecked items below cover the supported Git-version floor, ref-tip drift
detection, remaining collection and performance hardening, macOS Python
capability tests, the large-fixture exercise, and real-pipeline verification of
every frozen release artifact.

## 1. Objective

Implement the terminal-only Workfold MVP specified in `MVP.md` without
weakening its provenance or coverage guarantees. Build in vertical slices so a
usable default Git chart exists early, while the observation model and coverage
ledger are correct from the first slice.

The repository already establishes Python, argparse, a flat `workfold/` package, pytest,
Ruff, Pyright, uv, Nix, PyInstaller, Astro, and GitHub Actions. This is therefore
not a greenfield language decision: retain Python and the existing packaging and
release foundation.

No phase may add HTML output, a server, a TUI framework, a watcher, remote APIs,
duration estimation, a database, or persistent collection state.

## 2. Baseline and working rules

Starting baseline (retained here as historical planning context):

- `workfold` and `python -m workfold` were installable entry points;
- the CLI initially supported only help/version and a no-op invocation;
- packaging, tests, documentation, CI, PyPI publishing, frozen binaries, and
  Nix are scaffolded;
- runtime dependencies were initially empty;
- baseline tests and package/docs builds passed.

Implementation rules:

1. Preserve the module boundaries in this plan; terminal code must not leak into
   collectors or domain models.
2. Keep record counts, timestamp observations, and activity markers distinct in
   code, tests, summaries, and documentation.
3. Add the coverage-ledger invariant before adding a collector outcome that
   needs accounting.
4. Invoke Git with argv arrays and `shell=False`; repository-controlled text is
   never interpolated into a shell command.
5. Add fixture-backed tests with every collector feature. Do not test Git parsers
   only against hand-written happy-path strings.
6. Preserve nanosecond filesystem instants through collection and filtering.
7. Treat malformed/unreadable data as structured diagnostics, not generic log
   strings.
8. Keep `main(argv) -> int` independently testable. `__main__.py` remains only a
   process exit adapter.
9. Update public docs and CLI help in the same phase that exposes behavior.
10. A phase is complete only when its artifact-level smoke tests pass, not merely
    its unit tests.

## 3. Decisions fixed before coding

The implementation follows these resolved decisions from `MVP.md`:

- Git PATH selects the whole containing repository; filesystem PATH selects an
  exact root.
- `-t/--time` and `-m/--mode` independently select time and collectors;
  `this-week` and `git` are their defaults.
- `-p/--profile standard|portable|full` defaults to `standard` and never changes
  time or mode. Portable is Git-only object timestamp evidence; full expands
  the collectors selected by mode without changing report detail.
- Author/committer observations are atomic and coalesce only for the same record
  at the same instant.
- `Events` means activity markers after that one coalescing rule.
- `--git-commits-from` changes commit reachability only.
- File changes use root-versus-empty or commit-versus-first-parent tree diffs,
  with rename detection.
- Author filters apply to commit-derived records only.
- Ignore flags and `--exclude` affect filesystem collection only.
- Linux birth time uses a no-follow libc `statx` adapter with identity checking;
  POSIX ctime is never substituted.
- Overnight schedules, nested-repository auto-discovery, path-limited Git
  history, copy detection, and merge-per-parent expansion are deferred.
- Non-strict partial output may exit 0 only when useful results exist and the
  report prominently says partial; strict partial output exits 1.
- The chart uses one circle/square symbol per ordinary Git/filesystem marker,
  filled/hollow shapes for inside/outside status, and exact count tokens only
  when a cell is too dense for the available width.
- Sparse rows are globally aligned greedy half-open clusters. The first
  unassigned wall-clock event anchors a band, and later events never extend it.
- The default cluster window is `1h`; `30s` and `10m` remain supported custom
  windows, while chart band labels always stay at `HH:MM`.
- Marker order is local wall time, actual instant, canonical source (Git before
  filesystem for simultaneous ties), then stable marker ID; weekday only
  chooses the output column.
- Terminal output begins with the weekly matrix, then a content-aware symbol key,
  a left-aligned working-hours line, and exactly two summary statistic rows. The
  key names only visible categories and mentions `×N` only when rendered. Scope,
  period, successful coverage status, source breakdowns, and
  collector/author/extent/ignore/cluster metadata require `--verbose`, which
  also includes the detailed coverage ledger. `--coverage` prints only that
  ledger beyond the compact report. Partial collection, explicit narrowing,
  unsupported capabilities, and nonzero cropping remain default-visible
  exceptions.

If implementation reveals that one of these must change, update `MVP.md` and
record the reason before changing behavior.

## 4. Proposed package layout

```text
workfold/
  __init__.py
  __main__.py
  cli.py                    argparse surface and exit mapping
  application.py            orchestration of the full pipeline
  config.py                 enums, raw arguments, effective RunRequest
  models.py                 origins, observations, markers, classifications
  coverage.py               ledger, capabilities, structured diagnostics
  provenance.py             canonical stable IDs
  sanitization.py           safe terminal text
  time_ranges.py            timezone and date selector resolution
  schedule.py               grammar, normalization, classification
  aggregation.py            summaries, sparse clusters, crop accounting
  reports.py                renderer-neutral Report and chart DTOs
  collectors/
    __init__.py
    base.py                  collector protocols and CollectorResult
    git.py                   Git orchestration
    git_objects.py           raw commit/tag parsing
    git_changes.py           NUL-safe diff parsing
    git_reflogs.py           reflog enumeration/parsing
    filesystem.py            traversal and metadata extraction
    filesystem_times.py      platform capability adapters
    ignores.py               Git-standard discovery and explicit patterns
  renderers/
    __init__.py
    terminal.py              chart, legend, summaries, coverage
```

Tests mirror these boundaries:

```text
tests/
  fixtures/                 static parser bytes and expected snapshots
  support/
    git_repo.py             controlled temporary Git repositories
    filesystem.py           platform-aware metadata fixtures
    snapshots.py
  test_cli.py
  test_config.py
  test_models.py
  test_coverage.py
  test_time_ranges.py
  test_schedule.py
  test_aggregation.py
  test_git_*.py
  test_filesystem_*.py
  test_terminal_renderer.py
  test_end_to_end.py
```

Keep modules smaller if their boundary remains useful; do not create forwarding
modules solely to match this tree.

## 5. Runtime dependency plan

Retain `argparse` rather than adopting a second CLI framework. Use Rich only at
the terminal presentation boundary for measured layout, literal styled text,
and terminal capability handling. Clustering, source/schedule semantics,
coverage, and report DTOs remain Rich-independent. Repository-controlled values
must enter Rich as sanitized literal text, never as parsed markup. The focused
runtime dependencies are:

- `rich` for the terminal renderer, without introducing a TUI interaction
  model;
- `tzlocal` to resolve a named, historical-rule-capable operating-system zone;
- `tzdata` where the platform lacks a system IANA database, including frozen
  Windows artifacts;
- `pathspec` for documented Git-wildmatch filesystem exclusions rather than an
  incomplete custom glob implementation.

Git remains a runtime executable dependency for Git collection, not a Python
library dependency. Filesystem-only mode must work without Git when no Git
ignore semantics are available/requested.

Pin compatible major ranges in `pyproject.toml`, regenerate `uv.lock`, update
the Nix package dependencies, and verify PyInstaller hooks/data whenever a
runtime dependency is introduced.

## 6. Core technical contracts

### 6.1 Domain types

Use frozen, slotted dataclasses and string-valued enums where practical.

Required layers:

- `RecordOrigin`: exactly one semantic source record and deterministic ID;
- `TimestampSlot`: origin ID plus requested timestamp kind;
- `TimestampObservation`: one captured slot, integer UTC epoch nanoseconds, raw
  representation, offset, and actor metadata;
- `ActivityMarker`: one observation or an authorized same-record
  author/committer pair;
- `ClassifiedMarker`: marker plus selected-zone wall-clock fields and schedule
  result;
- `ClusterBand`: one greedy global half-open interval and ordered markers for
  each weekday cell;
- `Report`: renderer-neutral sparse chart bands, summaries, outside-list rows,
  capabilities, diagnostics, and coverage.

No domain model contains ANSI/Rich styles, preformatted table cells, subprocess
handles, or mutable global state.

### 6.2 Stable provenance

Use canonical length-delimited bytes and BLAKE2 or SHA-256. Never use Python's
randomized `hash()`.

At minimum, identities include:

- commit: repository identity + commit OID;
- file change: repository + commit + diff-basis parent + status + old/new path;
- tag: repository + tag ref + tag object/target OID;
- reflog: repository + ref + exposed old/new IDs + raw selector/timestamp +
  actor/message + deterministic duplicate ordinal;
- filesystem: lexical absolute root/path + entry type.

An observation ID adds timestamp kind. A marker ID derives from the ordered
observation IDs. Repository identity is canonical within one run and is not
claimed stable after a repository is moved.

### 6.3 Coverage ledger

Implement typed dispositions rather than free-form counter names:

- record: `eligible`, `ignored`, `explicitly_excluded`,
  `excluded_entry_type`, `semantic_git_admin`, `record_error`;
- extraction: `captured`, `unavailable`, `unsupported`, `error`;
- selection: `included`, `outside_date`, `author_filtered`;
- plotting: `marker`, `coalesced_into_marker`.

The ledger asserts the three equations from `MVP.md` at report construction.
An invariant mismatch is an internal fatal error even without `--strict`.

Diagnostics are structured objects with stable code, stage, target, optional
path/provenance, severity, and sanitized details. Strictness uses severity/code,
never string matching. One operational error may affect several timestamp
slots; keep operational diagnostic counts separate from slot disposition counts.

### 6.4 Collector boundary

Conceptually:

```text
Collector.collect(CollectorRequest) -> CollectorResult

CollectorResult {
  origins/observations: iterable or sequence
  coverage: collector ledger partition
  capabilities: structured capability records
  diagnostics: structured diagnostics
}
```

Collector requests contain only discovery and extraction settings. Common date
selection, author selection, role coalescing, localization, schedule
classification, display cropping, sparse clustering, and rendering remain
application pipeline stages.

The first implementation may materialize observations for simplicity, but the
API must allow streaming. The renderer-neutral sparse chart may retain the
lightweight marker references required to draw one symbol per event, but it must
not duplicate full collector records or provenance payloads. Summary counters,
coverage, and the most recent N outside-list rows remain separately bounded.

### 6.5 Injected environment

Inject these dependencies for deterministic tests:

- clock (`now()`);
- timezone resolver/database;
- `GitRunner`;
- filesystem metadata adapter;
- environment (`NO_COLOR`, `TERM`);
- terminal width/TTY capability;
- output streams.

## 7. Delivery phases

### Phase 0 — Freeze contracts and test harnesses

Goal: make every later collector outcome representable before collection code
lands.

Tasks:

- [x] Convert every option and default in `MVP.md` into an effective-config
      matrix.
- [x] Implement the redesigned argparse conflict/applicability matrix for
      repeatable `-t/--time`, `-m/--mode`, `-p/--profile`, and granular
      evidence selectors.
- [x] Define enum values, record IDs, timestamp kinds, dispositions, diagnostic
      codes, and exit mapping.
- [ ] Decide and document the minimum tested Git feature set after probing the
      Git versions present in development and CI; prefer capability detection
      where a command differs across versions.
- [x] Implement temporary Git-repository support using local identity/config and
      controlled raw author/committer dates.
- [x] Add helpers for annotated/lightweight tags, refs, reflogs, renames, merges,
      ignored files, and hostile filenames/messages.
- [x] Add deterministic clock, timezone, terminal, and output fakes.
- [x] Add a coverage-ledger property/helper that fails if any partition does not
      reconcile.
- [x] Replace the scaffold's blanket 100% coverage threshold with a documented,
      branch-aware project threshold of at least 90% once mutually exclusive
      OS adapters land. Keep ledger, date-range, schedule, coalescing, and option
      validation branches fully exercised; do not hide correctness branches with
      coverage pragmas merely to satisfy a number.

Tests:

- config expansion and every invalid flag pair;
- provenance determinism and cross-repository separation;
- empty and synthetic ledger reconciliation/failure;
- fixture helper self-tests proving stored Git dates/offsets are exact.

Exit gate:

> Every requested timestamp opportunity in the specification has a typed
> terminal disposition; no `other` or untracked fallback bucket exists.

### Phase 1 — Pure time, schedule, marker, and sparse-chart core

Goal: finish collector-independent correctness before invoking Git or walking a
filesystem.

Tasks:

- [x] Implement integer-nanosecond instants and source-preserving raw timestamp
      values.
- [x] Implement record, observation, marker, classified-marker, initial bin,
      and report models.
- [x] Implement local-zone resolution with `ZoneInfo`, `tzlocal`, and packaged
      timezone data where needed.
- [x] Implement current ISO week, repeated ISO-week union, inclusive/open
      calendar ranges, all dates, range normalization, and membership.
- [x] Route those existing range primitives through the unified `-t/--time`
      grammar (`this-week`, ISO weeks, `DATE..DATE`/open ranges, and `all`).
- [x] Implement schedule grammar, weekday expansion, interval union,
      half-open classification, and canonical formatting.
- [x] Implement same-record/same-instant author/committer coalescing.
- [x] Implement marker classification and summary aggregation with
      source/record-kind/inside/outside counts.
- [x] Implement explicit display ranges plus hidden-marker counts.
- [x] Replace fixed bins with a second-granularity `--cluster-window` CLI
      duration parser (default `1h`, positive and below 24 hours) while keeping
      event wall-clock instants nanosecond-capable.
- [x] Implement greedy globally aligned half-open bands anchored by the earliest
      unassigned visible wall-clock event; never chain-extend a band.
- [x] Render only occupied observed bands in implicit start-to-end day order,
      omit every empty band and boundary row, and derive one-hour gap cues from
      consecutive observed cluster endpoints.

Tests:

- valid/invalid ISO weeks, duplicates, adjacent and disjoint unions;
- open `DATE..DATE` ranges, inclusive end dates, reversed-range rejection;
- Europe/Berlin spring-forward and fall-back instants;
- schedule day ranges, multiple/overlapping/adjacent intervals, `24:00`, omitted
  weekdays, start-inclusive/end-exclusive boundaries, and rejected overnights;
- cluster-window compact/whitespace-separated duration grammar, anchored
  half-open boundaries, global weekday alignment, midnight/end-of-day behavior,
  `1h` default plus `30s`/`10m` overrides, and fall-back duplicate wall times;
- authorized and forbidden coalescing cases;
- mixed source/status marker ordering, crop, and ledger invariants.

Exit gate:

> Synthetic observations can flow through filtering, coalescing,
> classification, sparse clustering, and a renderer-neutral report with exact,
> reconciled counts.

### Phase 2 — First vertical Git quick view

Goal: satisfy the basic product question early with the default command.

Tasks:

- [x] Resolve existing input paths to unique containing repositories, including
      linked worktrees and bare-repository detection.
- [x] Implement a local-only `GitRunner` with fixed locale, disabled pager,
      prompt, external diff/textconv, and lazy fetch; use bounded stderr and
      structured failures.
- [ ] Snapshot the initial ref tips used by the scan and detect obvious ref drift
      before final coverage status.
- [x] Enumerate `HEAD` or all locally reachable commit OIDs with `git rev-list`,
      deduplicating by OID per repository.
- [x] Read commit headers through `git cat-file --batch` or an equivalently raw,
      machine-safe batch protocol. Parse author/committer headers from the right
      as epoch plus offset; preserve raw values.
- [x] Do not use `--since`/`--until` traversal pruning for author-date selection.
- [x] Emit default author observations, apply the current-week filter, classify,
      summarize, and build coverage.
- [x] Implement the first 80-column terminal chart, legend, summary, capability
      notes, and actionable non-repository error.
- [x] Escape control characters and ANSI from repository-controlled text.

Tests:

- commits on HEAD and non-current branches;
- one commit reachable from multiple refs counted once;
- controlled offsets and author/committer dates that cross selected boundaries;
- detached and unborn HEAD;
- repository paths containing spaces/non-ASCII;
- invalid commit headers, missing objects, Git not installed, and non-repository
  targets;
- no subprocess requests network or uses a shell;
- default output at exactly 80 columns and empty current-week output.

Exit gate:

> In a fixture and a real local repository, `workfold` renders unique
> all-local-ref commit author markers for the selected current week with a
> complete reconciled ledger or an honest partial status.

### Phase 3 — Complete date and terminal UX

Goal: finish common user controls before broadening collectors.

Tasks:

- [x] Expose `-t/--time` with the `this-week` default, individual/repeated ISO
      weeks, closed/open inclusive calendar ranges, and `all`.
- [x] Expose `-m/--mode git|fs|all` with `git` as the independent default.
- [x] Expose `-p/--profile standard|portable|full` with `standard` as the
      default, without allowing any profile to mutate time or mode.
- [x] Expose timezone, hours, display range, author filters, no-color, outside
      list, limit, coverage, strict, and verbose options.
- [x] Replace the initial `--bin` surface with `--cluster-window DURATION` and
      reject zero, negative, malformed, duplicate-unit, out-of-order, and
      24-hour-or-longer values while accepting optional whitespace between
      ordered components in one quoted argument.
- [x] Apply commit-author filters as case-insensitive literal OR matches.
- [x] Replace three-character density/source cells with one ordered event symbol
      per marker: green `●`/red `○` for Git inside/outside and blue `■`/red `□`
      for filesystem inside/outside.
- [x] Add width-aware exact count compaction (`●×N`, `■×N`, `○×N`, `□×N`)
      only for overloaded cells; never approximate, truncate, or hide a count.
- [x] Render globally aligned occupied bands with no empty/boundary rows and one
      dim duration cue for observed-endpoint gaps of at least one hour. Format
      band labels as `HH:MM` regardless of event/window precision while retaining
      exact seconds/nanoseconds in provenance and outside-event rows.
- [x] Move terminal layout/styling to a narrow Rich boundary while keeping Rich
      objects and markup out of reports and treating untrusted values as literal
      sanitized text.
- [x] Begin stdout directly with the matrix, followed by a content-aware symbol
      key without a heading, a left-aligned working-hours line, and a compact
      three-row statistic block without a heading: Events total, Schedule split,
      and Calendar split.
- [x] Move cluster/compression policy, exact collectors, authors, collection
      extents, scope, period, successful coverage status, source breakdowns, and
      ignore/exclusion policy behind `--verbose`.
- [x] Finish disjoint-range labels, zero-event percentages, default-visible
      nonzero crop counts and coverage exceptions, and omission of zero-valued
      subtype rows.
- [x] Retain the most recent N outside markers with bounded memory, then render
      them chronologically with exact offset and omitted count.
- [x] Handle redirected output, `NO_COLOR`, `TERM=dumb`, broken pipes, long
      paths, and hostile metadata without tracebacks or control injection.

Tests:

- full parser conflict matrix and exit codes;
- short/long time and mode spellings, `this-week`, repeated ISO weeks, all
  closed/open range combinations, and `all` end to end;
- mode/time/profile orthogonality, portable Git-only enforcement, and locked
  portable/full profile conflicts with granular selectors;
- author matching by name/email, repeat OR, and unaffected non-commit record
  behavior once those records arrive;
- sparse terminal snapshots at 80 and wider columns, with/without ANSI;
- every source/schedule symbol, mixed event sequences, exact overloaded-cell
  count tokens, occupied labels without boundary rows, omitted empty bands, and
  one-hour observed-endpoint gap cues;
- second-level marker/window fixtures whose band labels remain `HH:MM`, paired
  with exact outside-list/provenance assertions;
- no pre-table title/subtitle, followed by a visible-content symbol key and three
  direct Events, Schedule, and Calendar rows without a Summary heading;
- key entries only for visible Git/filesystem/outside categories, `×N` only when
  count tokens render, and an independent left-aligned working-hours line;
- verbose-only scope, period, full successful coverage, source breakdown,
  cluster/compression, collector, author, extent, and filesystem policy details;
- default-visible partial/filter/unsupported coverage and nonzero-crop notices;
- empty results that still begin with the matrix header and place the no-events
  state inside the matrix section;
- core compact-statistic rows retained at zero, zero-valued subtype rows omitted,
  and verbose context/operational details absent/present with the flag;
- bounded outside list ordering and truncation;
- non-TTY and broken-pipe behavior.

Exit gate:

> Every non-collector-specific CLI/output behavior in `MVP.md` is stable and
> snapshot-tested before exhaustive Git volume is introduced.

### Phase 4 — Exhaustive Git records

Goal: account for every supported local Git timestamp role and record kind.

Tasks:

- [x] Enable committer observations and same-record author/committer marker
      coalescing with separate coverage counts.
- [x] Derive file-change records with NUL-safe `git diff-tree` plumbing, root
      versus empty-tree and first-parent bases, and fixed rename detection.
- [x] Preserve old/new paths, similarity when Git exposes it, change status, and
      diff-basis parent in provenance.
- [x] Avoid one subprocess per commit; use batch/stdin plumbing where supported.
- [ ] Skip expensive file diff discovery when every inherited requested commit
      timestamp is provably outside the date scope, without making a global
      out-of-range file-count claim.
- [x] Enumerate local tag refs. Parse annotated tag objects and create an
      unavailable tagger slot for each lightweight tag.
- [x] Enumerate every reflog through Git. Because Git's reflog walk omits
      entries that point to non-commit objects, resolve semantic reflog storage
      paths through Git and parse those files directly when necessary. This is
      a narrow exception for reflog records, not filesystem-metadata scanning of
      `.git/`. Preserve raw date/offset, ref, actor, message, and object IDs.
- [ ] Distinguish `no reflogs available`, per-ref absence, expired history, parse
      failure, shallow repository, and missing promisor objects in capabilities
      and diagnostics.
- [x] Implement comma-separated
      `--git-records commit,file-change,tag,reflog`,
      `--git-commit-times author,committer`, and
      `--git-commits-from HEAD|all-local-refs` semantics.
- [x] Ensure tag/reflog collection remains independent of
      `--git-commits-from` scope and author filters.

Tests:

- differing and identical author/committer instants and offsets;
- root, ordinary, and merge commits;
- add/modify/delete/rename including tabs/newlines/non-UTF-8 path bytes;
- commit versus file-change versus combined-list totals and coverage;
- annotated and lightweight tags, aliases to one tag object, tagger offsets;
- HEAD, branch, remote-tracking, stash, and other available reflogs;
- disabled/empty/expired reflog cases supported by the fixture Git version;
- shallow/partial/missing object behavior with network prohibition;
- malformed raw objects and reflog output under strict/non-strict modes.

Exit gate:

> A controlled repository reconciles every requested commit, file, tag, reflog,
> author, committer, and coalesced observation without confusing records with
> markers or contacting a remote.

### Phase 5 — Filesystem collector

Goal: provide an honest current-snapshot filesystem view with standard Git
ignore behavior and explicit platform capabilities.

Tasks:

- [x] Normalize filesystem roots lexically, remove overlapping roots, and avoid
      resolving symlink targets.
- [x] Implement `lstat`/`DirEntry.stat(follow_symlinks=False)` discovery
      snapshots and integer `st_*_ns` values.
- [x] Implement regular-file quick scope and exhaustive directory/symlink scope;
      special files receive an explicit entry-type disposition.
- [x] Keep hard-linked paths distinct and never recurse through symlinks.
- [x] Prune Git administrative storage, bare-repository roots, nested worktrees,
      and submodules according to the specification.
- [x] Implement platform adapters:
      - POSIX mtime/ctime/atime;
      - Linux no-follow `statx` birth time with nanoseconds and device/inode
        identity validation against the discovery snapshot;
      - macOS/BSD real birth time;
      - Windows real creation time with ctime fallback only where Python
        documents it as creation;
      - explicit unsupported metadata-change or birth capabilities where no
        distinct API exists.
- [x] Delegate respect-mode file discovery to NUL-delimited Git semantics using
      tracked plus untracked/non-ignored entries. Tracked files must remain
      included even when current ignore patterns match.
- [x] Implement include-ignored traversal and the documented explicit pattern
      subset with directory pruning.
- [x] Account for vanished entries, permission errors, scandir failures, stat
      failures, capability-wide unsupported slots, and per-entry unavailable
      slots.
- [x] Annotate atime as potentially unreliable in every relevant report.

Tests:

- files, directories, symlinks, hard links, and special-file disposition;
- exact nanosecond mtime/atime where the platform preserves them;
- POSIX ctime labeling and absence of false creation values;
- platform-gated birth/creation tests with explicit skip reasons, including
  native Linux `statx` collection and symlink no-follow identity;
- disappearing entries and injected scandir/stat/permission errors;
- root/nested `.git` exclusion, worktree pointer files, bare roots, submodules;
- tracked-but-currently-ignored, nested ignore negation, info excludes, isolated
  global excludes, ignored/untracked entries, and include-ignored;
- explicit patterns, matched subtree accounting, and overlapping roots;
- filesystem-only operation where Git is unavailable/outside a repository.

Exit gate:

> Every discovered eligible filesystem entry and requested timestamp slot lands
> in exactly one ledger disposition on every tested platform.

### Phase 6 — Combined mode and evidence profiles

Goal: assemble portable Git-object and full selected-mode workflows and verify
conservation across collectors.

Tasks:

- [x] Union Git and filesystem observations without cross-source deduplication.
- [x] Finish source/record-kind report-breakdown reconciliation.
- [x] Verify combined-source sparse cells place individual Git and filesystem
      symbols in event order without a synthetic mixed-source token.
- [x] Implement `-p/--profile` with default `standard`, Git-only `portable`, and
      selected-mode `full` values.
- [x] Lock portable to commit author/committer plus annotated tagger evidence,
      with no file-change/reflog/filesystem or remote-publication semantics.
- [x] Implement exact selected-mode full expansion and conflict rejection; keep
      time and mode unchanged and label explicit author/exclusion narrowing.
- [x] Keep coverage accounting on every run; suppress ordinary successful status
      by default, surface exceptional status by default, and print the full
      grouped ledger for `--coverage` or `--verbose`. Keep profiles independent
      of report detail.
- [x] Derive complete/partial/failed status from typed diagnostics and ledger
      state.
- [x] Implement multi-path partial handling, strict final exit, and the
      no-usable-collector failure rule.
- [x] Detect/report ref or filesystem races where feasible and qualify the
      non-atomic scan.
- [ ] Add lightweight timing/process-count instrumentation behind verbose test
      hooks; do not expose a new product mode.

Tests:

- standard/portable/full profile expansion with every allowed and forbidden
  companion flag;
- repeated-week and explicit-range full scans without implicit time/mode changes;
- verbose scope/profile display plus default-visible author/exclusion Coverage
  exceptions;
- full-profile output without an implicit ledger, `--coverage` with ledger only,
  and `--verbose` with operational details plus the ledger;
- portable annotated-tag/commit evidence, lightweight-tag unavailability, and
  explicit absence of remote-publication claims;
- identical Git/FS path+instant preserved as distinct evidence;
- mixed cells and exact source totals;
- unavailable or unsupported birth time plus available other times remains
  accounted complete;
- one bad root/collector under non-strict and strict;
- every ledger equation on realistic combined fixtures.

Exit gate:

> `workfold . -t all -m all -p full` accounts for every supported requested slot,
> while `workfold . -t all -m git -p portable` reports only portable Git-object
> timestamp evidence; both qualify limitations and never claim more than the
> enabled collectors observed during the scan.

### Phase 7 — Hardening, documentation, and release readiness

Goal: make the implementation safe to ship across supported artifacts and
platforms.

Tasks:

- [x] Replace placeholder README/Astro copy with installation, quick start,
      time/mode/profile selection, CLI reference, coverage interpretation, Git
      semantics, filesystem capabilities, schedule syntax, privacy, and
      accuracy pages.
- [x] Keep `MVP.md` as the normative behavior contract and mark completed plan
      items; move durable implementation decisions into public docs.
- [x] Add `--help` accuracy notes and concise actionable examples.
- [x] Audit every subprocess argument, terminal string, diagnostic, and broken
      pipe path.
- [ ] Exercise a non-blocking large fixture or benchmark with approximately
      10,000 commits, 100,000 file-change records, and 100,000 filesystem
      entries. Record process counts and peak memory before setting any hard
      performance budget.
- [x] Ensure Git commit/header reading and file changes use batch processes; fix
      accidental one-process-per-record behavior.
- [ ] Add macOS Python tests to complement current Linux/Windows CI, with
      platform-specific filesystem capability assertions.
- [x] Update dependency audits and Nix dependencies.
- [ ] Smoke-test the installed wheel and frozen Linux/macOS/Windows binaries
      against deterministic fixture repositories and filesystem roots, not only
      `--version`.
- [ ] Verify frozen timezone data, Git invocation, Unicode, color policy, and
      exit codes.

Exit gate:

> Every acceptance criterion in `MVP.md` has a linked automated test or a
> documented platform capability result, and all release artifacts execute the
> real collection/render path successfully.

## 8. Test strategy across phases

### 8.1 Unit and property-style tests

Use table-driven pytest tests for deterministic value domains. Use randomized
or property-style generation only where it materially exercises conservation:

- arbitrary valid ledger partitions always reconcile;
- one altered count always fails reconciliation;
- normalized date-range unions never double-include a boundary;
- schedule interval unions preserve half-open classification;
- coalescing never crosses record ID, instant, granularity, or source.

Adding Hypothesis is optional; do not add it merely for tests that are clearer as
explicit boundary tables.

### 8.2 Git fixture repositories

Prefer plumbing commands and isolated temporary config so fixtures do not depend
on the developer's Git identity, signing, hooks, global excludes, locale, or
default branch. Use controlled `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` or
`commit-tree`. Never modify a real repository.

Fixtures must prove behavior for:

- multiple refs and repositories;
- author/committer raw offsets and rewritten dates;
- root/normal/merge trees and renames;
- annotated/lightweight tags;
- available and absent reflogs;
- filenames/messages with whitespace, newlines, non-UTF-8 bytes where the OS
  permits, and terminal controls;
- shallow/missing-object limitations without remote access.

Parser unit fixtures should retain raw bytes captured from known Git commands,
but integration tests remain the authority for command semantics.

### 8.3 Filesystem capability tests

Do not make a platform test pass by treating mtime/ctime as birth time. Each
adapter advertises capabilities and tests either:

- assert the real timestamp and label when supported; or
- assert the exact unsupported/unavailable disposition and a clear skip/capability
  reason.

Inject metadata adapters for error/race tests that cannot be made deterministic
with live permissions.

### 8.4 Renderer snapshots

Snapshot plain text as the authority. Test ANSI separately so content assertions
do not depend on escape codes. Required widths/states:

- exactly 80 columns and one wider terminal;
- empty and occupied-only sparse charts without synthetic day-boundary rows;
- greedy anchors, exact half-open cluster boundaries, globally aligned weekday
  cells, and local-time/instant/source/marker-ID ordering with weekdays used only
  as column groups;
- `1h` default clustering plus `30s`/`10m` overrides, with minute-only band
  labels and exact seconds/nanoseconds retained outside the chart;
- each circle/square and filled/hollow source/schedule combination, including a
  mixed sequence without a synthetic mixed marker;
- omitted empty bands, sub-hour gaps without a cue, and one dim annotated cue
  for gaps of at least one hour;
- exact per-symbol count compaction for cells overloaded at the selected width;
- crop counts and disjoint ranges;
- no color flag, `NO_COLOR`, `TERM=dumb`, and non-TTY;
- long/control-bearing identities, messages, refs, and paths;
- complete, limited-capability, partial, strict-failed coverage.

### 8.5 End-to-end and artifact tests

Run the CLI through its installed console script, module entry point, wheel, Nix
app, and frozen executable. Each artifact performs at least one fixture Git run
and one filesystem run. Assert stdout, stderr, and exit status separately.

## 9. Performance and resource guidance

Correctness takes precedence over unsafe date pruning. Specifically:

- author-date completeness may require inspecting every reachable commit header;
- filesystem date filtering requires metadata extraction for every eligible
  current entry;
- `-t all -m all -p full` is inherently proportional to available history and
  filesystem size.

“Fast” therefore means process-efficient and streaming, not constant-time:

- one `rev-list` and batch object reader per repository where practical;
- batch/stdin file-diff plumbing rather than one process per commit;
- NUL/raw protocols rather than reparsing human output;
- O(selected activity markers + sparse cluster bands + coverage dimensions +
  outside limit) report state, without duplicating full provenance payloads;
- bounded captured stderr and display strings;
- no full commit bodies or file contents retained;
- no persistent cache in the MVP.

Do not introduce silent record caps. If a future safety limit is necessary, it
must be explicit, configurable, visible in coverage, and incompatible with an
unqualified complete status.

## 10. Risk register

### Git date and traversal loss

Risk: Git date pruning follows committer semantics and can miss author dates.

Mitigation: enumerate reachable OIDs, batch-parse exact headers, then apply the
common date selector.

### Reflog portability

Risk: reflog enumeration/pretty placeholders differ by Git version, and commit
placeholders can be mistaken for reflog timestamps.

Mitigation: freeze a machine-safe command contract against supported Git
versions, preserve the raw reflog date/offset rather than `%ct`, add integration
fixtures, and capability-detect differences. Git-resolved reflog files may be
parsed only to cover entries (notably non-commit object updates) that Git's
reflog walk silently omits; they remain part of the semantic Git collector and
must never become filesystem timestamp noise.

### Filesystem creation time

Risk: platform fields differ, especially Windows ctime and Linux birth time;
Linux `statx` also requires a second metadata read after discovery.

Mitigation: explicit adapters/capabilities, integer nanoseconds, Linux
device/inode identity comparison, platform CI, and no cross-kind fallback.

### Ignore correctness and scale

Risk: custom walking can violate nested/negated/global Git ignore semantics or
descend into enormous ignored trees.

Mitigation: delegate default included-file discovery to Git; use explicit
walking only for include-ignored/exhaustive scope; define pruned subtree counts
honestly.

### Terminal injection and width

Risk: repository metadata can include ANSI/control characters and long fields.

Mitigation: centralized sanitization, plain-text snapshots, fixed core columns,
middle ellipsis, and construction of Rich literal text from untrusted values;
never parse repository-controlled text as terminal markup.

### Non-atomic collection

Risk: refs/files can mutate during a scan.

Mitigation: snapshot ref tips, reuse one discovery stat result for portable
timestamps, identity-check Linux's companion birth-time read, detect obvious
drift/races, report partial status when detected, and qualify completeness as
“during this scan.”

### Frozen cross-platform behavior

Risk: PyInstaller may omit timezone/package metadata or invoke a platform Git
differently.

Mitigation: real fixture runs from every binary artifact and explicit tzdata
bundling tests.

## 11. Acceptance traceability

| MVP acceptance area | Primary phase | Required evidence |
| --- | --- | --- |
| Default current-week Git chart | 2 | Git fixture + 80-column snapshot |
| Schedule/source/red/no-color | 1, 3 | shape + boundary ANSI/plain snapshots |
| Time selectors and DST | 1, 3 | unit and end-to-end selector tests |
| Git record/time selectors | 4 | repository integration ledger |
| Coalescing/provenance | 1, 4 | model + Git integration tests |
| Tags and reflogs | 4 | annotated/lightweight/reflog fixtures |
| Filesystem timestamp truth | 5 | platform adapter tests |
| Ignore and exclusion behavior | 5 | isolated Git-ignore fixtures |
| Combined mode | 6 | adjacent circle/square + reconciliation test |
| Evidence profiles | 6 | standard defaults + portable object-only + full selected-mode collection tests |
| Strict/partial failures | 5, 6 | injected failures + exit assertions |
| Sparse clusters/crop/list/80-column output | 1, 3 | renderer snapshots |
| Python/platform matrix | 7 | CI required jobs |
| Distribution/binary/docs | 7 | artifact smoke and docs builds |

## 12. Explicit post-MVP backlog

Do not pull these into an implementation phase unless the product contract is
reopened:

- Git history scoped to a subdirectory/path across renames;
- auto-discovery/recursion into nested repositories and submodules;
- per-parent merge file events or copy detection;
- overnight schedules, holidays, or schedule configuration files;
- JSON/CSV/HTML output or a renderer plugin CLI;
- a watcher, database, cache, or incremental index;
- remote API integration;
- duration/session/billing/productivity inference;
- arbitrary revision expressions or relative date syntax;
- commit-to-every-reaching-ref maps;
- source-content timestamp parsing.
