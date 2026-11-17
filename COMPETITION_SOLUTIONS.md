# PowerContext 2026 competition implementations

This branch implements three independent product tasks on the official
`2026-competition` branch. It retains the frozen baseline
`9db1753434162c0aa7c9f1189a1722e617880a52` in its Git history.

| Task | Product module |
| --- | --- |
| `source-01-table-evidence` | `powercontext.sources.csv_rows` |
| `retrieval-01-reranking` | `powercontext.builtin.artifacts.memory.http_reranker` |
| `memory-02-embedding-batches` | `powercontext.builtin.inference.coalescing` |

CSV records retain their original column order and string values, have
percent-encoded stable row identities, and use captured Sources and standard
TextEvidence projections for native persistence and citation readback.

The HTTP reranker uses the supplied AsyncClient, validates each batch response,
and selects global ranks by descending score with stable rank-based ties.
The embedding wrapper shares pending and executing requests, preserves input
order and per-call batch usage, isolates cancellation, and caches only successful
vectors in a bounded LRU.

## Validation

Prepare the official fixed Python 3.12 test environment as documented in
`competition/README.md`, then run from the repository root:

```bash
make -C competition install
make -C competition check-baseline
make -C competition test-csv test-rerank test-embedding
```

The three unchanged public suites passed 17, 10, and 8 tests respectively in the
official container configuration: one CPU, 2 GiB memory, no external network,
and read-only source/test mounts. Four additional boundary tests and fourteen
existing Source/observation/reranking tests also passed. Modified files passed
Ruff 0.15.7 lint/format and `git diff --check`.

Full `uv run --locked --python 3.12 prek run -a` could not complete because tool
downloads repeatedly failed. The full product suite and private evaluation,
including the HTTP reranking quality workload, were not run. Public test results
are local evidence and do not assert competition scores.

## Submission

Submit this private repository's HTTPS URL and the exact branch/commit from the
platform's version fields. The evaluator must have read access to the repository
and commit. The same commit implements all three code tasks; no upstream PR is
required for scoring these tasks.
