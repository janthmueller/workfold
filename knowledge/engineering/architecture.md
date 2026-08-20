---
type: Software Architecture
title: Wuf architecture
description: Module boundaries, dependency rules, and the event-processing pipeline.
tags: [engineering, architecture]
status: stable
---

# Wuf architecture

Wuf is a **layered modular monolith with capability-oriented adapters**. It
is one Python package, one process, and one distributable CLI. Top-level
packages express dependency direction, while Git and filesystem internals are
grouped by the capabilities they implement.

The design combines three ideas:

- modules hide decisions that can change independently, following
  [information hiding](https://doi.org/10.1145/361598.361623);
- Git, filesystem, operating-system, and terminal behavior remain at the edges
  of a technology-neutral domain and folding core, borrowing from
  [ports and adapters](https://alistair.cockburn.us/hexagonal-architecture);
- timestamp processing remains a sequence of explicit stages, following the
  [pipes and filters](https://www.enterpriseintegrationpatterns.com/patterns/messaging/PipesAndFilters.html)
  style.

Wuf does not reproduce every layer or abstraction from those patterns.
The boundaries exist to protect observable behavior and make the code easier
to navigate, not to add ceremony.

## Source tree

The repository uses Python's supported flat package layout: the import package
lives directly in `wuf/`, with no additional `src/` directory.

```text
wuf/
├── __init__.py
├── __main__.py
├── cli/                    # argparse, invocation, composition, process output
├── configuration/          # setting schema, typed options, TOML, resolution
├── domain/                 # observations, provenance, time, schedule, coverage
├── collection/
│   ├── filesystem/         # current metadata, discovery, ignore rules, statx
│   └── git/                # source facade, commits, changes, tags, reflogs
├── folding/                # classify, cluster, aggregate, and lay out markers
├── application/            # execute a request and build a neutral report
│   └── coverage/           # finalize source fragments with pipeline outcomes
└── reporting/
    └── terminal/           # Rich chart, prose, summary, and event list
```

The extra `wuf/wuf` visible from the repository parent is expected:
the outer directory is the Git project and the inner directory is the Python
import package. PyPA calls this a
[flat layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/).

## Dependency direction

Dependencies point toward stable data and policy. The permitted relationships
are:

| Package | May depend on |
| --- | --- |
| `domain` | `domain` and the Python standard library |
| `folding` | `domain`, `folding` |
| `configuration` | `domain`, `folding`, `configuration` |
| `collection` | `domain`, `collection` |
| `application` | `domain`, `folding`, `configuration`, `collection`, `application` |
| `reporting` | `domain`, `folding`, `configuration`, `application`, `reporting` |
| `cli` | every package; it is the composition boundary |

Two consequences are especially important:

1. Domain and folding code cannot import Git, filesystem, subprocess, Rich,
   argparse, or configuration-file implementations.
2. Collectors emit normalized observations and accounting; they cannot decide
   terminal layout or render output.

`tests/architecture/test_dependencies.py` parses imports and fails when a
change violates this graph. It also rejects module cycles, cross-imports
between the Git and filesystem adapters, third-party dependencies in the core,
and argparse imports outside `cli`. Directory names alone therefore do not
carry the architecture—the dependency test makes it executable.

## Processing pipeline

One invocation flows through these stages:

```text
CLI and layered configuration
            │
            ▼
resolve paths, timezone, time scope, schedule, and evidence scope
            │
            ▼
Git/filesystem record discovery and timestamp extraction
            │
            ▼
normalized, provenance-preserving timestamp observations
            │
            ▼
scope selection → role coalescing → schedule classification
            │
            ▼
weekday projection → time clustering → bounded aggregation
            │
            ▼
renderer-neutral report and reconciled coverage ledger
            │
            ▼
terminal renderer
```

The shared normalized observation is the central port between collection and
the rest of the program. Collection-specific records may contain additional
implementation state, but that state cannot leak into folding or terminal
formatting.

Each source also returns an immutable coverage fragment. The fragment owns
record discovery, timestamp extraction, scope matches, and materialization
failures. The shared pipeline independently counts received observations and
plotting outcomes, then a generic finalizer checks both sides of the boundary.
Adding a source therefore does not require a new application-level coverage
switch statement.

The report is also renderer-neutral. A future renderer may consume it, but the
current product exposes only terminal output and contains no web server or
browser application.

## Selection axes

Wuf keeps four independent questions separate instead of encoding all
scope in one flag:

| Axis | Question | Examples |
| --- | --- | --- |
| Evidence | Which intrinsic timestamp observations exist in the answer? | `git:commit:author`, `fs:directory:modified` |
| Discovery | Which records are searched? | paths, Git commit reachability, ignore policy, `--fs-exclude` |
| Query | Which discovered timestamps match? | time range, timezone boundaries, Git identity |
| Presentation | How is the answer displayed? | schedule, clustering, hidden days, list projection |

Every exact evidence identifier has the shape
`source:record:timestamp`. Git records use `commit`, `file-change`, `tag`, or
`reflog`; filesystem records use `file`, `directory`, or `symlink`. This makes
the filesystem collection request an exact entry-type × timestamp matrix, so
one request can collect regular-file mtime, directory birth time, and symlink
atime without reading unwanted timestamp slots.

Git refs deliberately remain a discovery scope. The same commit may be
reachable from several refs but is still one object and one commit record, so
ref names cannot be embedded in its event kind without duplicating evidence.
Tag and reflog selectors enable independent records; `--git-commits-from`
controls only the starting refs for commit-derived evidence.

## Package responsibilities

### Domain

`domain` owns facts and invariant-preserving value objects: canonical evidence
kinds, timestamp kinds, record provenance, exact UTC instants, observation
scope, schedules, Git ref scope, identities, and the coverage ledger. It
performs no source discovery and produces no terminal text.

### Collection

`collection.git` owns local Git subprocess protocols and Git-derived records.
Its subpackages separate commits, file changes, raw objects, tags, and reflogs.
They share repository and process machinery without exposing a second
parallel `git_core` hierarchy. `collection.git.evidence` is the single
source-level facade: it coordinates commit discovery, file-change derivation,
tag and reflog collection, exact Git selection, source coverage, and a compact
operational summary. Application code does not reproduce those dependencies.

Both Git runners prepend fixed local-only safety options. They disable pagers,
credentials, external protocols, and configured filesystem-monitor hooks, then
admit only the read commands required by their adapter. Repository
configuration therefore cannot turn collection into an interactive, remote,
or helper-executing operation.

`collection.filesystem` owns current-snapshot discovery and metadata. Ignore
integration is nested under this adapter because ignore rules affect only
filesystem discovery; they never filter historical Git records. Native Linux
birth-time access is an implementation detail of filesystem metadata.
`root_schedule` gives explicit paths stable, non-overlapping ownership and
orders dynamically discovered repository roots without letting callbacks
invalidate pending work. `inventory_metadata` resolves Git-inventory paths
component by component beneath an opened root descriptor; when that capability
is unavailable, the collector retains bounded Git ignore membership but uses
native traversal. `scan` defines the shared metadata protocols and directory
identity invariant used by both strategies. These boundaries keep ownership
decisions out of traversal callbacks and operating-system safety decisions out
of root orchestration. One prepared root is passed through explicit request,
sink, and service bundles, so scan policy, mutable accounting, and platform
mechanisms cannot be confused by a long positional interface.
`ignore.service` receives one complete inventory backend covering materialized,
streaming, and directory-aware views. Production and test adapters therefore
cannot accidentally replace only the view selected on one operating system.
Production Git-aware discovery is fail-closed: once its authoritative inventory
strategy is selected, an inventory failure becomes partial coverage rather
than a silent semantic switch to native discovery. The native strategy remains
an explicit internal seam for non-Git discovery and tests.

Capabilities carry a typed semantic kind separately from their human-readable
label. Diagnostics likewise separate source-specific codes, policy category,
semantic kind, severity, and completeness impact. Exit-status and report
policy never depend on matching diagnostic prose or an ad hoc code allowlist.

### Folding

`folding` accepts classified markers and turns them into the sparse weekly
layout. Band assignment, clustering, adaptive exact-count compaction,
projection, and portable temporary SQLite spill storage live here. It does not
know how timestamps were collected or how Rich will display the result.
Each compact chart marker retains a bit-mask signature of its exact evidence
kinds. The signature survives in-memory grouping, SQLite spill, run compaction,
and lazy cluster storage, so a renderer may distinguish commit, tag, reflog,
file-change, and filesystem timestamp roles without retaining full provenance.
It is also part of visual equivalence: simultaneous markers with different
evidence signatures are never grouped merely because their source and schedule
state agree.

### Application

`application` coordinates one use case: resolve the request, run enabled
collectors, classify observations, aggregate markers, reconcile coverage, and
construct the neutral report. A `CollectorServices` bundle supplies its
two source-level ports (Git and filesystem). Application code neither
constructs production adapters nor coordinates Git subcollectors nor uses
optional collaborators with hidden defaults.

The resolved `EvidenceSelection` is the single semantic event scope.
Configuration profiles are named event sets whose contents derive the enabled
collector sources. Exact `--events` selectors are their inline alternative;
neither representation owns time, commit reachability, or filesystem ignore
policy.
`CollectionPlan` projects it into an exact Git source request and filesystem
entry-type/timestamp work; source adapters do not reinterpret CLI profiles or
keep a second scope model. Filesystem record discovery remains one
root-level accounting partition because ignored candidates can be counted
without an extra stat and may therefore have an intentionally unknown entry
type. Timestamp extraction is partitioned by entry type, where the type is
necessarily known, preserving exact coverage without sacrificing the fast Git
inventory path.

The report contract retains only resolved scope, stable collection facts,
coverage, aggregation, and provenance. It does not retain `RunOptions` or raw
Git/filesystem results, and it does not flatten facts into terminal labels.
`ReportRequirements` makes the bounded detail needed by a renderer explicit
without importing terminal choices into the application layer. Architecture
tests prevent reporting code from bypassing this contract.

Completeness is also application policy. `CompletenessAssessment` combines
typed diagnostic facts, capabilities, explicit scope qualifiers, and ledger
outcomes into one renderer-neutral verdict. Terminal code formats that verdict
but cannot independently reinterpret collection success. Diagnostic storage is
bounded by composition rather than list inheritance, so every insertion passes
through the same truncation and exact-summary accounting.

### Configuration

`configuration` owns scalar setting precedence plus structured event-style
rules. Style rules reuse domain evidence selectors, remain separated by
global/local precedence layer, and compile into immutable visuals for every
supported marker evidence signature before collection begins. Configuration
validates one-cell symbols and terminal color values, but it does not inspect
events or create report output. A rule covers a coalesced marker only when it
covers every evidence role retained by that marker.

### Reporting and CLI

`reporting.terminal` is the output adapter. Only this package creates Rich
renderables, applies terminal styles, or turns report facts into scope and
coverage prose. Generic terminal-safe text handling is in
`reporting.sanitization` and is shared with configuration display.
Busy-cell count grouping also belongs here: the default preserves distinct
retained evidence signatures, while the optional visual projection merges only
identical resolved symbol/style pairs. The report and folding layers retain the
same fine-grained runs in either presentation mode.

`cli` is the outer composition boundary. It owns argparse, standard-stream
handling, effective configuration display, diagnostic output, process exit
codes, and production adapter construction. It translates the argparse
namespace into plain configuration values, builds `CollectorServices`, and
maps terminal preferences to `ReportRequirements` and `TerminalOptions`.
`__main__.py` remains a tiny delegation to this package.

## Tests

Tests mirror the architecture:

```text
tests/
├── architecture/           # import and package-shape contracts
├── unit/                   # isolated domain, folding, config, and rendering
├── integration/            # real Git/filesystem and application boundaries
├── end_to_end/             # complete user-visible invocations
└── support/                # deterministic repository fixtures
```

The distinction describes the scope of a test, not whether it is fast. Git
integration tests intentionally create local fixture repositories with exact
author and committer timestamps.

## Placement rules

When adding code, choose the package by the decision it owns:

- a new timestamp fact or invariant belongs in `domain`;
- a new Git or filesystem mechanism belongs below its collection adapter;
- a transformation of normalized events belongs in `folding`;
- orchestration across capabilities belongs in `application`;
- terminal layout or styling belongs in `reporting.terminal`;
- parsing a flag or printing a process diagnostic belongs in `cli`;
- loading, merging, or validating configurable values belongs in
  `configuration`.

Prefer specific names such as `revisions.py`, `metadata.py`, or `reconciliation.py`.
Do not introduce catch-all `utils.py`, `helpers.py`, `types.py`, `base.py`, or
`core/` modules. A large cohesive state machine is preferable to several files
with unclear ownership; split it only when the resulting responsibilities can
be named independently.

Wuf remains a modular monolith. Introducing a service, plugin system,
persistent database, or framework layer requires an actual product need rather
than anticipated reuse.
