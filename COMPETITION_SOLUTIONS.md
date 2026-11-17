# PowerContext 2026 competition implementations

This branch implements all ten product code tasks on the official
`2026-competition` branch, retaining the frozen baseline
`9db1753434162c0aa7c9f1189a1722e617880a52` in its Git history.

| Task | Implementation |
| --- | --- |
| `source-01-table-evidence` | Lossless CSV row Sources and citation readback |
| `source-02-pi-history` | Visible Pi branch events, immutable records, durable connector checkpoints |
| `source-03-pdf-retrieval` | Complete physical PDF pages, revision identities, raw-word FTS, financial heading reranking and real page continuations |
| `retrieval-01-reranking` | Strict batched HTTP scoring and global stable selection |
| `context-02-incremental-pack` | Canonical UTF-8/Base64 packs, verified prefix deltas and native prepare integration |
| `processing-01-count-trigger` | Strict optional journal-count admission using the native supervisor and workers |
| `memory-02-embedding-batches` | Concurrent request sharing, cancellation isolation, bounded LRU and batch usage |
| `memory-01-source-lifecycle` | Durable scope-local dependency DAGs, retryable native Memory state transitions, Topic filtering |
| `context-01-evidence-selection` | Query-aware empirical sentence windows, scientific identifier protection, exact origins and complete UTF-8 budget accounting |
| `retrieval-02-hybrid-search` | One native UNION statement for bounded FTS/vector candidates and current references |

Original Source evidence remains complete and independently readable. Retrieval
uses actual product persistence and public Memory paths. Implementations do not
read grading labels or precomputed answers and require no online model service.

## Validation

Use the unchanged Python 3.12 environment in `competition/README.md`:

```bash
make -C competition install
make -C competition check-baseline
make -C competition test
```

The ten unchanged public suites pass 95 tests in the official container
configuration: one CPU, 2 GiB memory, no external network, and read-only
source/test mounts. Additional tests cover Unicode pack reconstruction,
malformed wire data, long Pi output persistence, exact evidence intervals,
and restart/retry after actual native storage failures. Related product tests
exercise the original prepared-context behavior and Topic persistence.
Modified files pass Ruff 0.15.7 lint/format and `git diff --check`.

Independent external scoring of the five public PepsiCo questions finds all
eight annotated evidence pages at limit 20 (Recall@20 = 1.00, compared with 0.50
on the original submission). The minimum evidence token recall is 0.9962 and
minimum annotated full-page token F1 is 0.9793. This measures the public report;
it does not establish the hidden reports' page coverage or retrieval quality.

A separate SciFact **training** sample has 63 claims, three original abstracts
per claim, and both 1,400/1,800-byte budgets. Its 64-request validation partition
has mean evidence token recall 0.7598 and complete evidence coverage 0.6719
(original submission: 0.6213 and 0.5313). The other 62 requests are development
data. The validation partition was inspected during diagnosis, so these are
local diagnostic measurements, not an untouched benchmark or official result.
Every candidate runs offline with read-only source and receives only the query,
budget and original texts; annotation scoring runs separately on the host.
The prepared-candidate measurement supplies all three real stored Memory hits,
as required by the selection contract.

Five focused regression tests cover decimal statistics, empirical results versus
repeated objectives, distinct scientific identifiers, stable candidate ties,
financial headings, original FTS word forms and real adjacent-page citations.
Four reproduce failures on the original submission. They and the related
prepared-context/edge tests pass 83 tests in the fixed dependency image.

One existing SQLite Topic-vector profile-filter test also fails on the unchanged
original submission in the same fixed environment; it is not reported as passing.
A broader configuration test requires `typer`, which is absent from the fixed
competition dependencies. Repository-wide `uv run --locked --python 3.12 prek
run -a` fails its type-check hook because the configured `./.venv` lacks the
dependencies installed in the separate checking environment. Its unrelated
format edits to official competition files have been restored byte for byte;
the official assets and dependency files remain unchanged.
Ruff and focused type checks pass for all five modified Python files using
`ty check --python` with the complete separate checking environment.

An independent native 40-round replay reconstructs every context and measures
all protocol bytes, including the first full pack, Base64 and digests. It is a
local diagnostic workload, not the official QMSum scoring workload.

Public tests do not establish private FinanceBench/SciFact quality thresholds,
the official incremental cost result, or awarded competition points. Those
checks still require platform evaluation.

## Submission

Use `https://github.com/tcwangliye/FHW_power_context.git`, branch
`2026-competition`, and the exact published commit. The same commit implements
all ten code tasks. Repository preparation is verified separately through the
retained baseline history; upstream PRs are not required for these code tasks.
