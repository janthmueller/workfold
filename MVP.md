# Workfold CLI MVP specification

Status: implementation contract for the first usable release

## 1. Product outcome

Workfold is a local, terminal-only CLI that answers two questions:

1. When did discoverable work activity occur?
2. How much of that activity occurred inside or outside the user's configured
   working schedule?

Workfold collects discrete timestamp observations from local Git records and
the current filesystem snapshot. It converts every captured instant to one
selected timezone, folds every selected date onto a representative
Monday-through-Sunday week, classifies each event against a working schedule,
and renders the result as a terminal chart.

Workfold does **not** infer continuous work sessions, hours worked,
productivity, or billable time. A timestamp is evidence that an event was
recorded at an instant; it is not evidence of activity throughout an interval.

The MVP is successful when the default command gives a fast, honest glance at
the current week and `-t all -m all -p full` gives an auditable inventory of
every timestamp kind the enabled local collectors can discover.

## 2. MVP boundary

The MVP includes:

- a Python CLI named `workfold`;
- local Git collection through the installed `git` executable;
- current-snapshot filesystem metadata collection;
- timezone-aware date selection and schedule classification;
- terminal clustering, rendering, summaries, diagnostics, and an optional
  outside-hours event list;
- explicit coverage accounting for requested records and timestamp kinds.

The MVP intentionally excludes:

- HTML, webpages, web servers, browser or TUI interaction models;
- background watchers or reconstruction of past uncommitted editing sessions;
- remote hosting APIs or any network access during collection;
- work-duration, billing, productivity, or recommendation calculations;
- commit-content analysis or parsing date-like text from file contents;
- persistent databases, caches, or configuration files;
- GUI configuration.

The internal pipeline must not depend on terminal formatting. A later renderer
must be able to consume the same normalized and classified events without
changing collectors.

## 3. Normative vocabulary

The following distinctions are required for correct counting:

- A **record** is a discoverable source object, such as a commit, annotated tag,
  reflog entry, Git file change, or filesystem entry.
- A **timestamp slot** is one requested timestamp kind on a record. For example,
  a commit requested with `--git-commit-times author,committer` has an author slot
  and a committer slot.
- A **timestamp observation** is a successfully extracted, timezone-aware
  instant plus its exact source provenance.
- An **activity marker** is one normalized item presented to classification and
  the weekly chart. Two observations may become one marker only in the
  narrowly defined author/committer coalescing case below.
- A **cluster band** is one globally aligned, half-open wall-clock interval used
  by every weekday column. Bands are anchored greedily from the earliest
  unassigned visible marker rather than from fixed clock boundaries.
- A **cell** is one weekday's ordered activity markers within a cluster band.
- The **requested scope** is the product of selected paths, collectors, record
  kinds, timestamp kinds, refs, entry types, time selector, identity filters,
  ignore policy, explicit exclusions, and platform capabilities.

User-facing `Events` totals count activity markers. Coverage totals count records,
timestamp slots, and observations explicitly; these numbers are not
interchangeable.

## 4. The fold-everything invariant

For every enabled collector and target, Workfold must do one of the following
for each record and requested timestamp slot it discovers:

1. capture and include the observation;
2. capture it and account for the filter that removed it;
3. report that the timestamp is unavailable on that record;
4. report that the timestamp kind is unsupported by the platform adapter;
5. report an unreadable, traversal, subprocess, or parse error; or
6. account for a documented record-level exclusion before timestamp extraction.

No requested timestamp may disappear silently between discovery and the
coverage ledger.

The guarantee is deliberately bounded and the scan is not an atomic snapshot.
Workfold can be complete only for timestamps that remain discoverable through
the enabled collectors in the requested scope while that collector is running.
Concurrent ref or filesystem mutation is reported when detected but cannot be
eliminated without repository locking or a watcher. Workfold cannot recover an
unsaved edit, an overwritten earlier
mtime, a deleted untracked file, an expired reflog entry, or any event that was
never logged. “Logged date” means a timestamp exposed by a Git record or
filesystem metadata, not date-like text inside a file.

Time selection is part of the requested scope. Collectors may avoid materializing
records that are provably outside that scope only when doing so cannot hide a
requested timestamp kind. In particular, Git's committer-date traversal filters
must not be used to claim exhaustive author-date selection: a commit can have an
author date inside the range and a committer date outside it. Coarse candidates
encountered outside the selected range are counted as date-filtered, but a quick
date-scoped run does not promise a global count of every out-of-range record.

Workfold must never print an unqualified “100% complete.” The preferred status
is:

> complete for all discoverable timestamps in the requested scope

The same line must mention partial reads, explicit filters, and unsupported
platform capabilities when any are present.

## 5. Command and defaults

```text
workfold [PATH ...]

Selection:
  -t, --time SELECTOR          default: this-week; repeatable for ISO weeks
  -m, --mode git|fs|all       default: git

Evidence scope:
  -p, --profile PROFILE       standard|portable|full; default: standard
  --git-records KINDS         commit,file-change,tag,reflog
  --git-commit-times KINDS    author,committer
  --git-commits-from SCOPE    HEAD|all-local-refs
  --author VALUE              repeatable identity filter
  --fs-times KINDS            birth,modified,metadata-changed,accessed
  --fs-entries KINDS          file,directory,symlink

Filesystem filtering:
  --respect-gitignore
  --include-ignored
  --exclude PATTERN           repeatable Git-style pattern

Time and output:
  --hours SCHEDULE
  --timezone IANA_ZONE
  --cluster-window DURATION
  --display-hours HH:MM-HH:MM
  --no-color
  --list-outside
  --limit N
  --coverage                   detailed coverage ledger
  --strict
  --verbose                    context, operational details, and coverage ledger
```

The default invocation is equivalent to:

```text
workfold . \
  --time this-week \
  --mode git \
  --profile standard \
  --git-records commit \
  --git-commit-times author \
  --git-commits-from all-local-refs \
  --hours 'Mo-Fr 08:00-16:30' \
  --cluster-window 1h
```

It selects the current ISO week in the resolved local timezone and includes all
commit authors unless `--author` is supplied. Whenever filesystem collection is
enabled without an explicit ignore flag, its separate default policy is
`--respect-gitignore`.

### 5.1 Path semantics

- With no path, the target is `.`.
- Every path must exist. Relative paths are resolved from the process working
  directory without following symlink targets during filesystem traversal.
- For the Git collector, a path identifies its containing local repository.
  The MVP collects the whole repository; PATH is **not** a Git history pathspec.
  Multiple paths resolving to the same repository are collected once.
- For the filesystem collector, a path is the exact scan root. A regular file is
  a one-entry root. Overlapping roots are normalized so an entry is not counted
  twice.
- In `-m all`, the Git side therefore covers each containing repository
  while the filesystem side covers the explicitly selected roots. `--verbose`
  states the high-level scope and both extents.
- A selected path inside no Git repository is a fatal, actionable error when
  Git is the only requested mode: `not a Git repository; use -m fs or
  pass a path inside a repository`.
- Multiple targets are independent failure domains. In non-strict combined or
  multi-target runs, Workfold continues with usable targets and reports partial
  coverage. If no requested collector succeeds, the run fails.

This whole-repository Git rule is intentional for the MVP. Correct historical
subtree scoping across renames is deferred rather than approximated silently.

### 5.2 Option compatibility

`-t/--time` may occur once for `this-week`, a calendar range, or `all`.
Repetition is valid only when every supplied selector is an ISO week; those
weeks form a union. It is an error to mix an ISO week with another selector
kind. `-m/--mode` and `-p/--profile` each occur at most once.

The default `standard` profile uses the granular evidence selectors and their
defaults. `portable` and `full` are locked profiles. No profile changes the time
selector or mode:

- `-p portable` requires effective mode `git`. It rejects `--git-records`,
  `--git-commit-times`, `--git-commits-from`, filesystem selectors, and
  filesystem filters because its exact Git-object scope is fixed.
- `-p full` broadens only collectors already enabled by `-m`. It rejects
  granular Git or filesystem scope controls applicable to that mode, even when
  a supplied value would be redundant.

The portable and full profiles may be combined with `--author`, and the full
profile may be combined with `--exclude` when filesystem mode is enabled. Those
are explicit narrowing filters. Default output must surface that fact as a
compact coverage exception: author narrowing is
`commit-derived records explicitly filtered by author` and exclusions are
`explicit exclusions active`. The verbose `Scope` fact combines mode and
profile, for example `Git · portable`, and `--verbose` also names filter values.
Schedule,
timezone, cluster-window, display, listing, strictness, and report-detail
options never change the selected collection scope. The relationship between
`--coverage` and `--verbose` is defined in section 12.3.

Git-specific options are rejected in mode `fs`. Filesystem-specific ignore,
time, and exclusion options are rejected in mode `git`. `--limit` is rejected
unless `--list-outside` is enabled. `--git-commit-times`,
`--git-commits-from`, and `--author` require `commit` or `file-change` records
to be enabled; they are
rejected for a tag/reflog-only selection where they would do nothing. Parser
validation distinguishes explicit user options from dormant defaults. Repeated
author filters and repeated exclusion patterns form unions. Comma-separated
kind lists are normalized and deduplicated.

Invalid values or incompatible options exit with status 2 and a short usage
message. Workfold does not silently ignore an inapplicable option.
Only the canonical long names and documented short forms in this contract are
accepted; there are no compatibility aliases.

## 6. Time selection and timezone semantics

Workfold resolves the selected timezone before resolving any local calendar
selector.

```text
time-selector := this-week | YYYY-Www | DATE '..' DATE | DATE '..' |
                 '..' DATE | all
DATE          := YYYY-MM-DD
```

- `--timezone` accepts an IANA name such as `Europe/Berlin` and uses timezone
  database rules, including historical daylight-saving transitions.
- Without the flag, Workfold resolves the operating system's named local zone.
  If it cannot obtain a DST-capable local zone, it asks for `--timezone` rather
  than silently substituting the machine's current fixed offset.
- `-t this-week` is the default. It begins Monday at 00:00 local time and ends at
  the following Monday at 00:00 local time.
- An ISO selector such as `-t 2026-W31` validates the ISO year/week
  combination. Repeated ISO selectors form a union of distinct half-open local
  weeks; they do not fill gaps.
- A calendar selector uses `START..END`, for example
  `-t 2026-07-01..2026-07-31`. Both user-facing dates are inclusive.
  `START..` and `..END` are valid open ranges; at least one endpoint is required.
- `-t all` has no time bounds.

Internally, selectors are represented as a normalized union of half-open
instant ranges. A closed range begins at local midnight on `START` and ends at
local midnight following `END`; open endpoints remain unbounded. This prevents
double counting at adjacent boundaries and correctly handles days that are not
24 hours long.

Every captured observation is normalized to an aware UTC instant while its raw
source timestamp and original UTC offset remain in provenance. Date filtering
uses the selected timezone's local calendar interpretation. Folding and schedule
classification happen only after that conversion.

During a fall-back transition, two distinct instants may map to the same local
weekday and cluster band; both remain separate events in that cell. A
spring-forward wall-clock interval may naturally have no events. Workfold does
not invent or discard events to make DST days look uniform.

## 7. Source behavior

### 7.1 Git source

Git collection invokes the installed `git` executable with argv arrays and
machine-safe, NUL-delimited or raw-object formats. It never invokes a shell,
fetch, pull, or remote API. Collection disables pagers, prompts, external diffs,
text conversion, and lazy fetching of missing promisor objects. A missing object
in a shallow or partial clone is an accounted limitation/error, never a reason
to contact a remote. Filenames are decoded with the operating system's lossless
filesystem strategy rather than dropped on invalid Unicode.

#### Commit reachability

- `--git-commits-from HEAD` selects commits reachable from `HEAD`.
- `--git-commits-from all-local-refs` selects commits reachable from `HEAD` and
  every locally present `refs/*` namespace, including local branches, tags,
  remote-tracking refs, stash, and other local refs. It does not contact their
  remotes.
- Commits reached through more than one ref are deduplicated by object ID.
- A commit is not assigned to one arbitrary branch. The MVP records the selected
  reachability scope and object identity, but does not compute a potentially
  expensive map of every ref that can reach every commit.
- Reflog-only unreachable commits are not silently treated as ref-reachable
  commit records; requested reflog entries still appear as reflog events.

An unborn `HEAD` with no commits is an empty successful repository result, not a
parse error.

#### Commit and file-change records

`--git-records commit` emits one logical event per selected commit timestamp.
`--git-records file-change` emits one logical event per file change and selected
commit timestamp. A comma-separated `commit,file-change` selection preserves
both granularities as separate events.

A commit's file changes are defined as the tree difference against its first
parent, or against the empty tree for a root commit. A merge commit is compared
with its first parent. Rename detection uses Git's standard rename heuristic;
when Git reports a rename, Workfold preserves it as one rename event with old
and new paths. Other kinds are added, modified, deleted, or other.

Git stores snapshots rather than an authoritative list of human actions. The
first-parent diff rule is therefore part of Workfold's provenance and must be
stated in help and documentation.

Nonzero commit and file-change totals are displayed separately in verbose
breakdowns when both are relevant. Reporting must never multiply or relabel
file events as commit counts, and it omits a subtype whose count is zero.

#### Author and committer observations

`--git-commit-times author,committer` applies to commit and derived file-change
records. Its comma-separated list accepts either or both kinds. The default Git
view selects `author`; the portable profile and Git-enabled full profile select
both.

For each role, preserve:

- the commit object ID;
- exact stored epoch and UTC offset;
- normalized instant;
- author and committer names/emails;
- commit subject;
- the role identifying the timestamp.

If author and committer observations for the same logical commit or file-change
record resolve to the exact same instant, Workfold may coalesce them into one
activity marker. That marker contains both complete observations. Coverage
still reports two captured observations and one observation coalesced for
plotting. Equal wall-clock text with different instants is not coalesced.

#### Record kinds

`--git-records` accepts a comma-separated list of:

- `commit`: reachable commit records;
- `file-change`: per-commit derived file-change records;
- `tag`: local tag records;
- `reflog`: available local reflog entries.

The default is `commit`. Lists are deduplicated without changing their semantic
set; there is no special `all` value because the full profile owns exhaustive
expansion.

For annotated tags, create one record per tag ref and capture the tag object ID,
ref name, tagger instant and offset, tagger identity, and subject/message
summary. Two tag refs remain distinct records even if they point to the same tag
object. A lightweight tag has no independent tagger timestamp; count it as a
discovered tag with an unavailable tagger slot. Never borrow the target commit's
timestamp.

When reflogs are requested, enumerate every reflog exposed by Git, including
HEAD, branches, remote-tracking refs, stash, and other available logs. Preserve
the ref, old/new object IDs when exposed, entry timestamp and offset, actor, and
message. Disabled, absent, or expired reflogs are an honest capability/result,
not a fabricated empty history. Coverage distinguishes refs with an available
reflog, refs without one, and captured entry counts where Git can expose that
distinction.

`--git-commits-from` controls commit reachability only. Tag and reflog record
collection uses the locally available tag and reflog namespaces when those
record kinds are requested.

#### Identity filtering

Each repeatable `--author VALUE` is a non-empty, case-insensitive literal
substring matched against commit author name or email. Values are ORed. The
filter applies to commit and derived file-change records only; annotated tags
and reflog entries are not commit authors and remain unaffected. The timestamp
role does not change the filter: a committer-date observation is still filtered
using the commit author. `--verbose` says either `all commit authors` or lists
the active filters, and explicitly says tags/reflogs are unfiltered when they
are enabled. Removed observations remain accounted in coverage.

### 7.2 Filesystem source

The filesystem collector describes one current snapshot. It does not claim to
reconstruct historical metadata values or deleted entries.

`--fs-entries` accepts a comma-separated combination of `file`, `directory`,
and `symlink`, normalized and deduplicated. Its standard-profile default is
`file`. Under a filesystem-enabled full profile, Workfold also considers
directories and symbolic links as metadata-bearing entries.
Special files stay out of scope and are counted by entry-type disposition.
Traversal never follows symlink targets, and hard-linked paths remain distinct
path records. Each event preserves the scan root, source path, entry type, raw
timestamp, normalized instant, and timestamp kind.

The requested `--fs-times` kinds are:

- `modified`: mtime, labeled `fs_modified`;
- `birth`: real birth/creation time only, labeled `fs_created`;
- `metadata-changed`: POSIX inode/status-change time, labeled
  `fs_metadata_changed`;
- `accessed`: atime, labeled `fs_accessed` and `potentially unreliable`.

Default filesystem evidence uses `birth,modified`. A comma-separated list is
accepted, normalized, and deduplicated. A filesystem-enabled full profile
requests all four kinds.

Creation time must come from a real creation/birth-time field exposed by a
supported platform adapter. On Linux, Workfold requests `STATX_BTIME` through
libc `statx`, retains integer nanoseconds, and uses `AT_SYMLINK_NOFOLLOW`. The
birth result is combined with the discovery stat snapshot only after device and
inode identity agree. On macOS/BSD the adapter uses `st_birthtime`; on Windows
it uses `st_birthtime` where available or the documented Windows creation field
on older supported Python versions. POSIX ctime is never labeled creation time;
filesystem metadata-change time is unsupported on Windows when the adapter
cannot expose a distinct value. If the runtime lacks the platform API globally,
coverage marks the kind unsupported. If the API is available but an individual
filesystem or entry does not return a birth-time field, coverage marks that slot
unavailable.

Filesystem reads are race-prone: an entry can disappear or change between
directory enumeration and metadata extraction. Such cases are counted as read
or stat errors, retained in diagnostics, and made fatal by `--strict`.

#### Git ignore semantics

Inside a Git worktree, the default `--respect-gitignore` policy includes:

- tracked entries, even if a current ignore rule matches them;
- untracked, non-ignored entries;
- standard repository, nested `.gitignore`, `.git/info/exclude`, and applicable
  global Git excludes as evaluated by the installed Git executable.

`--include-ignored` includes ignored entries as well. Outside a Git worktree,
`--respect-gitignore` has no rules to apply; Workfold states that and scans all
otherwise eligible entries.

`--respect-gitignore` and `--include-ignored` are mutually exclusive. Exact
ignored-entry counts are collected for the detailed coverage ledger when
feasible.
`--verbose` states the active policy without making an unsupported global count
claim.

Every repository's Git administrative path is a semantic exclusion. Workfold
prunes `.git` directories and worktree `.git` pointer files even under
`--include-ignored` and the full profile. Bare-repository storage is never
scanned as ordinary filesystem activity. Relevant dates come through semantic
Git records instead. A nested worktree or submodule is a traversal boundary; its
contents are scanned only when it is passed as its own filesystem root.

Each `--exclude` uses Git-wildmatch-style syntax relative to each filesystem
scan root, with `/` as the separator. A pattern without `/` matches a name at
any depth. Negation/re-inclusion is not supported in the MVP. Patterns are ORed,
apply after discovery, and override tracked or ignore inclusion. A matching
directory is recorded as one excluded subtree and pruned; Workfold does not
claim to know the number or timestamps of descendants it deliberately did not
enumerate. Exclusions affect filesystem entries only; they do not rewrite
historical Git commit or file-change truth. The default coverage exception says
`explicit exclusions active`, and `--verbose` names the active patterns.

### 7.3 Combined mode

`-m all` unions Git activity markers and filesystem activity markers after
each collector has completed its own discovery, extraction, provenance, and
coverage accounting. It then applies the common date, timezone, classification,
clustering, summarization, and rendering stages.

Git and filesystem evidence is never cross-deduplicated, even when instants and
paths happen to match. Verbose output names every enabled high-level source and
may show nonzero source-specific subtype counts.

## 8. Evidence profiles

The time selector, mode, and evidence profile are independent axes.
`-p/--profile` accepts `standard`, `portable`, or `full` and defaults to
`standard`. A profile never changes `-t/--time` or `-m/--mode`.

### 8.1 Standard evidence

The `standard` profile uses the explicit granular evidence selectors and their
documented defaults. It is the ordinary fast view and the only profile in which
those selectors may redefine record, timestamp, reachability, and filesystem
entry scope.

### 8.2 Portable Git-object evidence

The canonical portable-history command is:

```text
workfold PATH... -t all -m git -p portable
```

The `portable` profile requires Git mode and selects evidence whose timestamp
is stored in a Git object:

- reachable `commit` records from `all-local-refs`;
- both `author` and `committer` commit timestamps;
- local `tag` records and annotated-tag `tagger` timestamps.

Lightweight tags remain discovered/accounted but have no tagger timestamp.
The portable profile excludes derived file-change markers, reflogs, and
filesystem metadata. Those sources depend on a selected interpretation, one
clone's local state, or the current filesystem snapshot rather than being
independent Git object timestamps.

“Portable” does not mean “published.” Workfold does not contact a remote and
cannot infer when, whether, or to which hosting service a commit or tag was
pushed. Remote-tracking refs are merely locally present reachability roots.
Without `-t all`, the ordinary selected time scope still applies.

### 8.3 Full evidence in the selected mode

The canonical exhaustive local command is:

```text
workfold PATH... -t all -m all -p full
```

The `full` profile expands evidence only inside the already selected mode:

- when Git is enabled: `commit,file-change,tag,reflog`, author and committer
  times, and commits from `all-local-refs`;
- when filesystem is enabled: every supported filesystem timestamp kind,
  regular files/directories/symlinks without following targets, and ignored
  entries included.

Therefore `workfold -p full` means full Git evidence for `this-week`, because
`git` and `this-week` are the independent defaults. It is not shorthand for
`-t all` or `-m all`.

The full profile may be combined with commit-author and filesystem exclusion
filters. Those filters produce a compact default coverage exception;
`--verbose` shows `full` in Scope and lists the filter values. Author filters
affect only commit-derived records. Raw Git administrative storage remains
excluded by definition.

## 9. Coverage ledger

Coverage is maintained on every run. A normal, fully successful run does not
repeat its complete status in default output. Default output must instead print
a compact coverage exception whenever collection is partial, explicit filters
narrow the requested evidence, or a requested platform capability is
unsupported. Errors and other limitations must likewise never disappear merely
because neither detail flag was supplied.

`--coverage` prints the detailed ledger without operational details. `--verbose`
prints the full coverage status even for a normal success, operational context,
and that same ledger. Evidence profiles, including `full`, do not alter report
detail.

The ledger has separate, reconcilable phases.

### 9.1 Record discovery

For each collector/target/record kind:

```text
records discovered
  = records eligible for timestamp extraction
  + records excluded by ignore policy
  + records excluded by explicit pattern or entry type
  + records excluded as Git administrative/nested-repository boundaries
  + records that failed before extraction
```

Git reachability deduplicates repeated object IDs emitted for the selected
scope before commit extraction. It does not claim a count of refs reaching each
commit because the MVP deliberately does not construct that map. Multiple
selected paths deduplicated to one repository/root are reported as target
normalization, not as lost events.

### 9.2 Timestamp extraction

For every eligible record and requested kind:

```text
timestamp slots requested
  = observations captured
  + slots unavailable on an individual record
  + slots unsupported by the platform adapter
  + extraction/parse errors
```

### 9.3 Filtering and plotting

```text
observations captured
  = observations included after filters
  + observations outside the selected date ranges
  + observations removed by identity filters

observations included after filters
  = activity markers plotted
  + observations coalesced into another marker
```

Additional counters may refine these equations, but may not blur phases or
double-count one terminal disposition.

The detailed report groups counts by source, record kind, and timestamp kind.
It includes at least:

- unique Git commits, duplicate emitted object IDs, and duplicate target
  normalization;
- author and committer slots/captures/filtering/coalescing;
- annotated and lightweight tags, tagger captures, and unavailable tagger slots;
- reflogs available/unavailable and entries captured;
- filesystem entries by type;
- mtime, birth time, ctime, and atime requested/captured/unavailable/unsupported;
- ignore, entry-type, and explicit-exclusion record counts when enumerated;
- date- and identity-filtered observations;
- traversal, subprocess, stat, decode, and parse errors.

Collectors and timestamp kinds outside the effective request are labeled `not
requested` in detailed scope output rather than shown as zero captured.

Atime lines always carry `potentially unreliable`. Capability notes distinguish
global unsupported kinds from per-record unavailable values.

Coverage status is:

- **complete in requested scope** when every enabled target finished and all
  omissions are accounted as configured filters, unavailable values, or known
  platform limitations;
- **partial** when enumeration aborted, output could not be parsed, or any
  requested record/timestamp failed to read;
- **failed** when configuration is invalid or no requested collector produced a
  usable result.

Known unsupported timestamp kinds do not become fabricated errors, but they
must remain visible in the default exception text as well as detailed output.

## 10. Schedule and classification

The default schedule is Monday-Friday, 08:00 through 16:30. Saturday and Sunday
have no working interval and are entirely outside working hours.

The schedule grammar is:

```text
schedule  := clause (';' clause)*
clause    := day-set SPACE interval (',' interval)*
day-set   := day | day '-' day
day       := Mo | Tu | We | Th | Fr | Sa | Su |
             Mon | Tue | Wed | Thu | Fri | Sat | Sun
interval  := HH:MM '-' HH:MM
```

Day tokens are accepted case-insensitively in canonical two-letter form or
common three-letter English form. Canonical output uses the two-letter tokens.
Day ranges must move forward in Monday-Sunday order
and do not wrap across Sunday. Times use 24-hour notation; `24:00` is accepted
only as an interval end. An interval must have start before end and remain within
one day. Duplicate, overlapping, and adjacent intervals are normalized to their
union. Overnight intervals are deferred; users express their two sides as
clauses on adjacent days.

Examples:

```text
Mo-Fr 08:00-16:30
Mo-Thu 08:00-16:30; Fr 08:00-14:00
Mo-Fr 08:00-12:00,13:00-16:30
```

Classification uses the exact localized event time and half-open intervals:
start is inside and end is outside. Classification occurs before clustering, so
one cluster band may contain both inside- and outside-hours events. Weekend count
means local Saturday or Sunday regardless of whether a custom weekend interval
exists.

## 11. Sparse clustering and display range

`--cluster-window DURATION` defaults to `1h`. A duration is one or more integer
components using `h`, `m`, and `s`; components appear in that order and a unit
appears at most once. Whitespace is optional between complete components, but
not between an integer and its unit, so a spaced value must be one quoted CLI
argument. Examples include `30s`, `10m`, `1h`, `1h5m`, and `'1h 5m'`. The
normalized total must be positive and less than 24 hours.

Clustering is deterministic and operates on localized wall-clock time after date
selection, timezone conversion, schedule classification, and display cropping:

1. Sort all visible markers globally by local wall-clock time, actual normalized
   instant, canonical source order (Git before filesystem) for simultaneous
   ties, and stable marker ID. Weekday selects a column; it is not a sort key.
2. Choose the earliest unassigned marker's exact local time of day as the next
   band anchor.
3. Form the half-open band `[anchor, anchor + cluster-window)`, clamped at the
   visible range end, and assign every unassigned marker from every weekday
   whose local time of day falls in that interval.
4. Repeat from the next earliest unassigned marker.

The anchor never moves and a later marker does not extend a band. A marker
exactly at the end belongs to the next band. Because the same greedy bands are
used across all seven columns, Monday through Sunday remain vertically
comparable even when only one weekday caused a band to exist. Within each cell,
markers retain the same local-time, actual-instant, canonical-source, and stable
marker-ID ordering.

The chart is sparse rather than proportional. It renders only occupied cluster
bands in increasing time-of-day order; day ordering from `00:00` toward `24:00`
is implicit, and empty start/end boundary rows are not emitted. A band label is
the observed span from its first marker to its last marker, not the full
assignment window. Both endpoints are deliberately formatted as `HH:MM`; chart
labels never add seconds, even when events or the cluster window have
second-level precision. If both formatted endpoints are equal, one `HH:MM`
value is sufficient. Exact seconds and nanoseconds remain in normalized
provenance and exact outside-event rows. Between consecutive occupied bands, a
gap of at least one hour from the preceding last-observed time to the next
anchor is represented by exactly one dim, duration-labelled cue. Shorter gaps
have no cue, and the `HH:MM` labels remain authoritative for row ordering at
their documented minute precision in both cases. Gap cues are a renderer policy,
not events or aggregation buckets.

`--display-hours START-END` accepts a non-overnight half-open range with the same
time syntax as schedule intervals. It changes only chart visibility; it never
changes date filtering or inside/outside classification. Cropping is applied
before clustering, without rounding the requested bounds. A marker at the start
is visible and one at the end is hidden. A compact exception notice after the
summary reports exact nonzero marker counts hidden before and at/after the
explicit range. Source splits belong to verbose detail; zero-valued splits are
omitted.

## 12. Terminal report

The default output is a deterministic seven-column matrix followed by a compact
content-aware symbol key and three direct statistic rows. Exception-only coverage and crop notices
may follow when needed. An optional verbose block, outside-hours list, and
detailed coverage ledger may also follow. It must remain usable at 80 columns.
Wider terminals may expand labels but may not reveal information that is
entirely absent at 80 columns.

Terminal output begins directly with the matrix header and occupied band rows;
no product title, selected-range line, or chart subtitle precedes the table.
For an empty result, the same matrix header comes first and its body contains the
no-events state rather than replacing the table with a preamble.
Immediately after the matrix, Workfold prints only the mappings needed by the
visible matrix, the configured working hours on an independent left-aligned
line, and then the compact summary. There is no `Legend` heading or separate
default metadata block.

### 12.1 Event symbols and overloaded cells

The ordinary chart draws one single-column symbol for each activity marker, in
cell order:

| Marker | Source | Schedule state | Color when enabled |
| --- | --- | --- | --- |
| `●` | Git | inside working hours | green |
| `○` | Git | outside working hours | red |
| `■` | filesystem | inside working hours | blue |
| `□` | filesystem | outside working hours | red |

Circles versus squares preserve source, while filled versus hollow shapes
preserve schedule state. Those two shape dimensions remain meaningful without
color and avoid `G`, `F`, `M`, density bands, and a cell-wide outside marker.
Mixed-source or mixed-schedule cells simply contain the corresponding event
symbols next to one another. Coalesced identical Git author/committer
observations remain one activity marker and therefore one symbol, with both
roles preserved in provenance and coverage.

The renderer uses the available weekday-column width before compacting. When a
cell cannot show one symbol per marker within its bounded continuation layout,
it switches that cell to exact per-symbol count tokens such as `●×12 ■×4 ○×2`.
Compaction may discard within-band ordering, but it never uses approximate
density thresholds, truncates a count, or changes summary and coverage totals.
The symbol key explains count tokens whenever at least one cell is compacted.

Color is disabled by `--no-color`, by the presence of the standard `NO_COLOR`
environment variable, by `TERM=dumb`, or when stdout is not a TTY. Workfold
emits no ANSI styling in those cases; there is no force-color flag in the MVP.
The four shapes retain the complete source/schedule distinction under every
no-color condition and avoid relying on the red/green distinction alone.

Rich is the terminal presentation boundary: it measures and styles trusted
chart primitives and sanitized text, but no Rich object or markup string enters
the normalized model, collectors, classifier, clusterer, or renderer-neutral
report. Repository-controlled values are added as literal text and are never
interpreted as Rich markup.

### 12.2 Compact symbol key, summary, and exception notices

The symbol key is derived from markers actually visible after display cropping,
not merely from enabled collectors. Git, filesystem, and outside-hours items
appear only when their corresponding visible markers exist. It adds the `×N`
exact-count meaning only when at least one rendered cell is compacted. The key
has no `Legend` heading; long item lists wrap from column one rather than using
a hanging indent. The active schedule follows as `Working hours: ...` on its
own left-aligned line. Sparse clustering and compression policy are operational
details, not key content.

For example, a visible Git-only matrix containing inside- and outside-hours
events but no compacted cell prints:

```text
● Git · ○ Outside working hours
Working hours: Mo-Fr 08:00-16:30
```

If filesystem markers are also visible, `■ Filesystem` and the applicable
outside symbol join the first line. `×N exact count` is appended only when `×N`
tokens are visibly present in the matrix. A source represented only by outside
markers is named directly with its outside symbol.

The compact statistics include:

```text
Events    1,284
Schedule  1,067 inside (83.1%) · 217 outside (16.9%)
Calendar  1,210 weekday (94.2%) · 74 weekend (5.8%)
```

There is no `Summary` heading. `Events` is the common total. `Schedule` partitions
that total into inside and outside the configured working intervals; `Calendar`
independently partitions it into weekdays and weekends. The two partitions answer
different questions, so a weekend event may also be outside the schedule. Every
count remains visible when it is zero. Source/record breakdowns, Scope, Period,
and the normal complete Coverage status are verbose context rather than default
statistic rows. The working schedule appears beside the symbol key instead of
being repeated in the statistics.

`Events` counts activity markers after the defined coalescing. Both percentage
pairs use Events as their denominator, and each row's two percentages sum to
100% apart from display rounding. Valid markers are always classifiable;
malformed timestamps belong in coverage errors rather than the denominator. A
zero-event result prints `0` core counts and `n/a` percentages.

The compact layout does not authorize silent omissions. After the statistics,
default output appends a compact notice for nonzero chart cropping and a compact
Coverage notice for partial collection, explicit author/exclusion narrowing,
unsupported capabilities, or other non-ordinary limitations. Unsupported
capabilities are named directly rather than reduced to an opaque count. A fully successful,
unnarrowed run with supported requested capabilities prints neither notice.

Repeated weeks are displayed as a week union, not a misleading continuous
range. Open and all-history labels appear under `--verbose`. The three statistic
rows, visible-content symbol key, working-hours line, and any applicable exception notice may not
disappear at 80 columns.

### 12.3 Verbose operational details

`--verbose` appends a context and operational-details block after the compact
summary. It contains:

- Scope: every enabled high-level source plus the selected profile;
- Period: selected range plus timezone;
- the full Coverage status, including the ordinary successful wording;
- nonzero source and record-kind breakdowns that reconcile to Events;
- the cluster window and sparse empty-time compression policy;
- exact Git record/time/reachability selection and exact filesystem
  timestamp/entry selection;
- all-author scope or the active author filters, including which record kinds
  an author filter does not affect;
- resolved repository and filesystem-root extents;
- filesystem ignore policy and explicit exclusion patterns.

These details never replace or precede the compact summary. `--verbose` also
prints the detailed coverage ledger after its operational details. Default
output shows only exception-level coverage wording and does not print filter
values or collector plumbing. `--coverage` prints the same detailed ledger
without enabling the verbose context/operational block. The `full` profile
changes collection scope only and enables neither output mode.

### 12.4 Outside-hours list

`--list-outside` appends a table sorted by normalized instant ascending and then
stable provenance ID. It includes:

- exact localized timestamp and UTC offset;
- source and timestamp role(s);
- repository or root;
- abbreviated commit ID when present;
- ref when relevant;
- subject, reflog message, or path/change description.

`--limit N` defaults to 50, must be positive, and bounds rows globally. When the
full set is larger, Workfold retains the most recent N outside markers, prints
that subset in ascending chronological order, and states how many older rows
were omitted.
Long fields are truncated at 80 columns without removing timestamp, source, or
identity columns.

## 13. Normalized model and pipeline boundary

Collection, accounting, and plotting use three distinct immutable layers. The
conceptual model is:

```text
RecordOrigin {
  record_id: deterministic stable identity within this collection snapshot
  source: git | filesystem
  record_kind: commit | git_file_change | annotated_tag | reflog |
               filesystem_entry
  repository_or_root: path
  paths/ref/object/actor/description metadata as applicable
}

TimestampObservation {
  observation_id: record_id + timestamp kind
  origin: RecordOrigin
  kind: git_author | git_committer | git_tagger | git_reflog |
        fs_created | fs_modified | fs_metadata_changed | fs_accessed
  instant_utc_ns: integer UTC epoch nanoseconds
  raw_timestamp: exact source representation
  original_offset_minutes: optional integer
  actor_name: optional string
  actor_email: optional string
}

ActivityMarker {
  marker_id: deterministic identity derived from constituent observations
  occurred_at_utc_ns: shared instant for all observations
  observations: one or more TimestampObservation
}

ClassifiedMarker {
  marker: ActivityMarker
  local_datetime: aware datetime in selected zone
  weekday: Monday..Sunday
  local_time_of_day: nanosecond-capable wall-clock value
  within_schedule: boolean
  weekend: boolean
}

ClusterBand {
  start_time_of_day: first observed wall-clock time and assignment anchor
  end_time_of_day: last observed wall-clock time for the label/gap calculation
  cells: ordered markers for each Monday..Sunday column
}
```

An `ActivityMarker` has more than one observation only when author and committer
observations from the same logical record have the same instant. Atomic
observations remain the unit of coverage. Markers remain the unit of charts,
classification, percentages, and outside lists. Filesystem nanoseconds are kept
in the integer instant. Sparse chart labels intentionally stop at minutes, while
outside-event rows and normalized provenance retain exact seconds and available
nanoseconds.

Record and marker IDs use canonical bytes plus a stable digest such as BLAKE2 or
SHA-256; Python's randomized `hash()` is forbidden. Commit IDs are unique only
within a repository. File-change identity includes commit, diff basis, status,
and old/new paths. Reflog identity includes ref, old/new IDs, raw timestamp,
actor/message, and a deterministic duplicate ordinal. Filesystem identity uses
the lexical absolute path and entry type without resolving a symlink target.

Processing order is fixed:

1. parse CLI values and validate option compatibility;
2. resolve paths, repositories, timezone, time/mode selectors, evidence profile,
   schedule, entry types, and ignore policy into one immutable run request;
3. discover requested records and update record coverage;
4. extract requested timestamp slots and update extraction coverage;
5. normalize timestamps without overwriting raw provenance;
6. apply date, identity, ignore, entry-type, and explicit filters at their
   documented stages and account for every removal;
7. coalesce only eligible identical author/committer observations;
8. localize and classify included markers;
9. apply display cropping, build globally aligned sparse cluster bands, and
   account for hidden markers;
10. build a renderer-neutral report model;
11. render the matrix, compact symbol key and summary, optional verbose operational
    details, outside list, and detailed coverage according to the output flags.

Collectors do not import terminal rendering code. Renderers do not invoke Git,
read filesystem metadata, interpret date selectors, or classify schedules.

## 14. Diagnostics, privacy, and exit status

All collection is local. Workfold must not make network requests, invoke remote
Git operations, or send telemetry.

Normal reports go to stdout. Actionable warnings and fatal diagnostics go to
stderr. Paths, identities, subjects, and reflog messages are untrusted terminal
text: control characters and embedded ANSI sequences are escaped or replaced
before rendering. Raw subprocess commands use argv arrays and never invoke a
shell with repository-controlled strings. A downstream broken pipe exits
quietly without a traceback.

Exit statuses are:

- `0`: valid run completed, including an empty result or an accounted platform
  limitation; a non-strict partial multi-target result may also return 0 but must
  show `Coverage: partial` prominently;
- `1`: operational failure, no usable requested collector, or any partial-read /
  parse / traversal error under `--strict`;
- `2`: invalid CLI syntax, incompatible flags, invalid path/configuration,
  missing required Git executable, or invalid timezone/schedule/date value.

`--strict` does not stop at the first recoverable entry error. Workfold should
continue far enough to produce the most complete ledger it safely can, render a
prominent partial report when usable data exists, and then exit 1.

## 15. Accuracy disclosures

Help and public documentation must state:

- commits and metadata are discrete markers, not work duration;
- Git author and committer dates can differ and can be rewritten;
- file changes are derived from tree differences, not stored human actions;
- annotated tags have independent tagger dates; lightweight tags do not;
- reflogs are local, optional, and expiring;
- filesystem values describe only the current snapshot and can change through
  copying, checkout, extraction, formatting, builds, or access;
- filesystem creation time is not universally available;
- ctime is metadata-change time on POSIX, not creation time;
- atime may be disabled, delayed, or changed by other programs;
- deleted untracked files and earlier mutable metadata values are unrecoverable;
- past uncommitted edit sessions require a future watcher and are outside the
  MVP.

## 16. Acceptance criteria

The MVP is complete only when all of the following hold:

1. `workfold` in a local repository renders current-week author timestamps for
   unique commits reachable from all local refs in a Monday-Sunday/time chart.
2. Exact event classification uses the default `Mo-Fr 08:00-16:30` half-open
   schedule. Git/filesystem events use circle/square shapes, outside events use
   hollow red shapes, and `NO_COLOR` retains the source and schedule distinction
   without ANSI escapes.
3. `-t this-week`, ISO week unions, inclusive/open `DATE..DATE` ranges, `-t all`,
   invalid combinations, timezone conversion, and at least one spring-forward
   and one fall-back boundary are tested.
4. `--git-records commit,file-change` granularities and
   `--git-commit-times author,committer` roles retain correct provenance and
   separate record/marker totals.
5. Identical author/committer instants coalesce only as specified and reconcile
   two observations to one activity marker in coverage.
6. Annotated tagger timestamps, lightweight-tag unavailability, all available
   reflog namespaces/entries, and missing/expired reflog behavior are accounted.
7. `-m fs` captures every requested platform-supported current metadata kind,
   including Linux `STATX_BTIME` where returned, never calls ctime creation time
   on POSIX, never follows symlinks, and accounts for unavailable birth times.
8. Default Git-ignore behavior includes tracked and non-ignored untracked files;
   include-ignored, entry-type expansion, explicit patterns, nested rules,
   info/global excludes, and Git-admin pruning are tested.
9. `-m all` preserves source separation, renders individual Git and
   filesystem symbols together without a synthetic mixed marker, and never
   cross-deduplicates evidence.
10. `-p portable` is Git-only object timestamp evidence and makes no remote
    publication claim. `-p full` expands exactly within the independently
    selected time/mode, changes no report-detail setting, supports
    identity/exclusion narrowing honestly, and `-t all -m all -p full`
    reconciles record, slot, observation, and plotted-event counts. The detailed
    ledger appears only with `--coverage` or `--verbose`.
11. Non-strict and strict partial failures have the documented coverage status,
    diagnostics, and exit codes.
12. Greedy assignment windows are globally aligned and half-open, only occupied
    observed bands are labelled, empty bands are omitted, one-hour gaps between
    observed endpoints are compressed, overloaded cells use exact count tokens,
    and chart labels stay at `HH:MM` without losing exact provenance/list
    precision. Terminal output starts with the matrix, content-aware symbol key,
    configured working-hours line, and three compact statistic rows without a
    summary heading. The key contains
    only visible categories and mentions `×N` only when exact count tokens appear;
    Scope/Period/full-success Coverage, source breakdowns, and operational
    collector/author/extent/policy/cluster details appear only under
    `--verbose`. Partial collection, explicit narrowing, unsupported
    capabilities, and explicit display cropping remain visible by default as
    exception notices. `--list-outside` is stable and bounded, and snapshots
    remain readable at 80 columns.
13. Unit and fixture-repository integration tests pass on Python 3.10-3.14 and
    supported Linux/Windows CI jobs; platform capability tests skip only with an
    explicit reason.
14. Wheel, source distribution, frozen binary, Nix package, and Astro docs build
    successfully, with CLI smoke tests against installed artifacts.
