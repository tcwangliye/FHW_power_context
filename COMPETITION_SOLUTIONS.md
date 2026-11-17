# PowerContext competition implementations

Repository: `https://github.com/tcwangliye/FHW_power_context.git`

Branch: `2026-competition`

Frozen baseline: `9db1753434162c0aa7c9f1189a1722e617880a52`

| Task | Product implementation |
| --- | --- |
| CSV | Lossless row Sources and original-record readback |
| Pi history | Branch ancestry, complete events and durable checkpoints |
| PDF retrieval | All physical pages, immutable Sources, stored-page ranking and exact citations |
| HTTP reranking | Strict response validation, batching and stable global ordering |
| Incremental context | Bounded UTF-8/Base64 deltas and complete reconstruction |
| Count trigger | Durable new-source counting and native worker admission |
| Embeddings | Concurrent batching, cancellation isolation and bounded caching |
| Source lifecycle | Persistent dependencies, native deactivation and recovery |
| Evidence selection | Query-directed literal windows with precise offsets and full output budgeting |
| Hybrid search | One bounded SQL query for full-text and vector candidates |

## PDF retrieval

Pages are parsed from the complete original PDF and stored through the public
Source/Runtime/Memory path. Ranking combines native full-text search with all
active PDF entries returned by the public Memory listing API. This prevents a
global top-100 candidate cutoff from excluding a report's evidence. Company
names in document IDs are matched across punctuation and digits; explicit
company queries prioritize that company's reports. Financial concepts include
cashflow, margins, capital intensity and segment performance. Short financial
headings may contain units. Continuations use actual stored neighboring page
citations, with duplicate physical document/revision/page identities removed.

The complete stored-page scan is linear in the collection size. It is intended
for report collections at the competition scale, not an unmeasured scalability
claim for arbitrarily large libraries.

## Evidence selection

The selector preserves exact source text, Python character offsets, scope and
revision identities. The trust policy, envelope, JSON, citations and text all
count toward the UTF-8 byte budget. Oversized sentences receive local query
windows even when a short introduction also matches the query; introductory
matches cannot suppress evidence near the end of a longer result sentence.
Ordinary context preparation remains the default.

## Verification

Run the unchanged official suites using the fixed competition dependencies:

```bash
make -C competition docker-test
```

The ten public suites pass 95 tests. Related product regression suites pass
85 tests, including multi-report company matching after restart, duplicate-page
citations, and long result sentences following matching introductions. These
three defect scenarios fail on the previous product submission.

Independent diagnostics use original public FinanceBench PDFs and SciFact TRAIN
abstracts. Candidate processes receive only original inputs, queries and
budgets; annotation scoring runs separately. Three additional reports contain
467 pages and six questions. A separate sample contains 120 previously unused
training claims, three abstracts per claim and both 1,400/1,800-byte budgets;
every candidate is stored through the real Memory API. These developer checks
are not official competition results.

Changed Python files pass Ruff lint/format and focused type checking. Full
pre-commit runs retain repository-wide type diagnostics; this is not reported
as a clean full-repository check. Official tests, data and dependency files
are unchanged in the submission.

Submit the repository URL, branch and complete published commit for each
selected task. Official points require the authenticated competition platform
to rerun all functional and quality gates.
