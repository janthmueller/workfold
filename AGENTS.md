# Wuf repository guidance

- Read `knowledge/index.md` and only the linked concepts relevant to a task
  before making substantial product or architecture changes.
- Keep `README.md` and `docs/src/content/docs/` concise and user-oriented.
  Put implementation rationale, design decisions, and agent bookkeeping in the
  OKF bundle under `knowledge/`.
- Treat `wuf --help`, tests, and implemented behavior as authoritative. Update
  affected public docs and knowledge concepts when behavior changes.
- A roadmap entry is not evidence that a feature exists.
- Preserve the flat Python package layout: production code lives directly in
  `wuf/`, not under a `src/` wrapper.
