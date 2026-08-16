# Workfold benchmarks

These are opt-in end-to-end measurements, not ordinary correctness tests. A
benchmark starts a complete Workfold process and includes configuration,
collection, folding, terminal rendering, and the local Git subprocesses it
uses.

## Run a suite

From the repository root:

```bash
uv run python -m benchmarks.run ~/linux --suite quick
uv run python -m benchmarks.run ~/linux --suite scope --json benchmarks/results/linux.json
uv run python -m benchmarks.run --fixture medium --suite complete
```

The built-in suites are:

- `quick`: standard Git, filesystem, and combined current-week runs;
- `scope`: those three modes for both current-week and all-date selection;
- `profiles`: portable and full evidence profiles with bounded and all-date
  selections;
- `complete`: every named case.

Repeat `--case NAME` to select an exact subset instead. Run `--help` for the
case names and process controls. `--workfold-executable PATH` measures an
installed script or release binary instead of `python -m workfold` from the
checkout.

## Measurements

Each recorded sample includes:

- monotonic wall time;
- child user and system CPU time where the platform exposes it;
- the Workfold process's kernel-recorded high-water RSS on Linux;
- sampled peak RSS summed across the Workfold process tree on Linux;
- minor and major page faults and voluntary/involuntary context switches;
- exit status, timeout state, stdout/stderr sizes, and a stdout digest;
- the exact `Events` summary count.

Every workload adds `--strict`, so incomplete collection fails instead of
becoming a successful timing sample. The runner also rejects missing summaries,
changed event counts, or changed stdout across repetitions. Raw JSON records Python, Workfold, Git,
OS, CPU, load, total memory, Workfold checkout revision/dirty state, target
HEAD/tracked dirty state, all-ref commit count, Git object-store statistics,
commands, cache policy, and every individual sample.

Main RSS is the stable high-water mark for Workfold itself. Summed tree RSS also
captures concurrently running Git children, but it is not proportional-set
size: shared pages can appear in more than one process. Sampling defaults to 5
ms, so extremely short-lived child peaks can be missed. Non-Linux hosts report
memory as `n/a`; timing and output validation still work.

Warmups are explicit and the runner never drops the operating system's page
cache. Compare results only under the same host, target, command, sample
interval, and warmup policy. Do not use fixed cross-machine timing thresholds
in the normal test workflow. Global and system Git configuration are disabled
for repeatability; repository-local configuration still applies.

## Synthetic fixtures

`--fixture small|medium|directory-heavy|large` creates an automatically removed repository with
historical and current-week commits, distinct author/committer identities,
annotated tags, reflogs, tracked files, untracked files, ignored files,
directories, and symlinks where supported.

The `directory-heavy` fixture places one tracked, untracked, or ignored file in
each directory. It isolates directory traversal and parent-handle validation
costs that file-dense fixtures can hide:

```bash
uv run python -m benchmarks.run --fixture directory-heavy --case fs-all
```

To keep a fixture for profiling tools, create one explicitly in a new or empty
directory:

```bash
uv run python -m benchmarks.fixture /tmp/workfold-medium --size medium
uv run python -m benchmarks.run /tmp/workfold-medium --suite complete
```

Fixture generation refuses non-empty targets and never deletes an existing
repository.
