---
type: Product Roadmap
title: Wuf roadmap
description: Candidate product directions and their delivery order.
tags: [product, roadmap]
status: draft
---

# Wuf roadmap

This document records possible directions for Wuf. It is a planning aid, not a
promise that every item will ship or that the proposed CLI names are final.

## Product guardrails

- Keep collection, normalization, classification, and presentation separate.
- Selectors decide which evidence is collected; views only decide how the same
  normalized events are presented.
- Preserve provenance and coverage accounting for every collector.
- Keep privacy-sensitive or potentially expensive collectors opt-in.
- Do not turn discrete timestamps into claims about hours worked, productivity,
  or continuous activity.
- Keep the default invocation fast, legible, and compatible with an 80-column
  terminal.

## Configurable event styles

Allow symbols and colors to be configured with the same selector vocabulary
used for events. More-specific rules should override broader rules, with clear
built-in fallbacks for unmatched events.

The styling model must continue to communicate source, event kind, and
inside/outside-schedule state in color terminals and under `NO_COLOR`. Adding a
new collector must not require special-case rendering logic.

Questions to settle before implementation:

- Which visual property communicates event kind and which communicates schedule
  classification?
- How are conflicting selector rules resolved and shown by `--show-config`?
- Which source colors and symbols should be built-in defaults?

## Additional views

Introduce a view boundary that consumes the same selected, normalized events.
The current folded matrix remains the default.

Candidate views:

- `fold`: the existing representative Monday-Sunday by time-of-day matrix.
- `timeline`: exact chronological dates, timestamps, provenance, and event
  descriptions without folding dates together.
- `calendar`: actual weeks as rows and weekdays as columns, retaining when work
  occurred across the selected period.
- `breakdown`: compact counts grouped by source, event kind, identity, or
  schedule classification.

The relationship between a primary timeline view and the existing optional
`--list` output needs an explicit UX decision so the two features do not become
overlapping ways to request the same information.

## Shell events

Add an opt-in `shell` source, initially covering timestamped Bash, Zsh, and Fish
history. Shell events should enter the same normalized model and be selectable
through the event-selector grammar, for example conceptually as `shell:*` or
`shell:bash:*`.

Correctness and privacy requirements:

- Collect only timestamps actually recorded by the shell; never invent times
  for untimestamped commands.
- Account for untimestamped, malformed, truncated, unavailable, unflushed, and
  unreadable history in coverage reporting where observable.
- Do not associate a command with a selected project unless its history record
  genuinely contains a working directory or equivalent provenance.
- Treat ordinary history without directory metadata as global shell activity.
- Do not display complete command text by default because commands may contain
  credentials, tokens, private paths, or other sensitive arguments.
- Do not interpret a recorded command duration as measured productive time.
- Deduplicate only when two records can be proven to describe the same history
  observation; commands from separate sessions remain separate evidence.

## Possible later collectors

After the shell collector establishes a reusable non-project-local source
boundary, investigate:

- editor activity with durable, documented timestamps;
- build and test result records;
- package-manager and system-operation histories;
- other local development tools with reliable timestamped records.

Each collector must have a precise scope, provenance model, privacy review, and
coverage semantics before being added. Wuf should not parse arbitrary date-like
text or quietly treat caches and heuristics as authoritative activity records.

## Suggested delivery order

1. Improve the README and published documentation for the features that already
   exist.
2. Add selector-based configurable event styles.
3. Establish the shared view interface and add the timeline view.
4. Add shell events vertically, with Bash, Zsh, and Fish fixtures and coverage
   tests.
5. Add the multi-week calendar view.
6. Reassess later collectors based on real usage.

Every feature should include CLI and configuration documentation, `--show-config`
support where applicable, unit and integration coverage, terminal snapshots for
rendering changes, and an accuracy note for limitations that users could
otherwise misinterpret.
