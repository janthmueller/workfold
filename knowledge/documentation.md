---
type: Documentation Policy
title: Documentation boundaries
description: Defines the audience and responsibilities of Wuf's README, public docs, and internal knowledge bundle.
tags: [documentation, maintenance]
status: stable
---

# Purpose

Wuf keeps user documentation separate from project bookkeeping so a person
learning the CLI does not have to read implementation history or agent context.

# Surfaces

| Surface | Audience | Content |
| --- | --- | --- |
| `README.md` | A first-time visitor | Product purpose, installation, first commands, and links onward |
| `docs/` | Wuf users and contributors | Task-oriented guides, user-facing semantics, accuracy limits, and concise contribution instructions |
| `knowledge/` | Maintainers and coding agents | Architecture rationale, complete behavior notes, implementation constraints, decisions, and roadmap material |
| CLI `--help` | A user of the installed version | The authoritative option names, accepted values, and built-in defaults |

“Internal” means maintainer-facing, not confidential. The repository is public;
never place credentials, private user data, or other secrets in the bundle.

# Rules

- Lead public pages with what the user can accomplish.
- Prefer examples over exhaustive implementation explanation.
- State limitations when they affect interpretation, safety, privacy, or a
  successful command.
- Keep architecture mechanics, module placement rules, performance internals,
  release plumbing, and design deliberation in this bundle.
- Do not duplicate a complete CLI reference across the README and public site.
  Link to `wuf --help` for the exact installed surface.
- When behavior changes, update the CLI help and tests first, then the relevant
  public task and internal concept.
- Roadmap concepts describe candidates, not shipped behavior.

# Maintenance

The bundle root and subdirectory `index.md` files provide progressive
disclosure. Every other Markdown file in the bundle carries OKF frontmatter
with a non-empty `type`. Record meaningful structural or semantic updates in
`log.md`; ordinary typo fixes do not need log entries.
