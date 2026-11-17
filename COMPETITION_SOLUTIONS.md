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
names in document IDs are matched across punctuation and digits. Unambiguous
acronyms are resolved from the supplied report text. Company identity and fiscal
year tokens do not compete with substantive evidence terms in body ranking.
Financial concepts include cashflow, liquidity, margins, tax, capital intensity
and segment performance. Actual short headings are distinguished from prose
references; matching statements and segment tables receive dedicated positions.
The remaining budget favors direct matches over neighboring pages.
Continuations use actual stored page citations, with duplicate physical
document/revision/page identities removed.

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

Before filling remaining space, the selector compares disjoint windows from
the same exact entry. It charges both citations and the JSON separator, and
scores their union without rewarding repeated sentences. This prevents a
large contiguous window from consuming the space needed for a second result.

## Verification

Run the unchanged official suites using the fixed competition dependencies:

```bash
make -C competition docker-test
```

The ten public suites pass 95 tests. Related product regression suites pass
86 tests, including multi-report company and acronym matching after restart,
statement headings versus matching narrative, duplicate-page citations, long
result sentences and disconnected evidence under a shared budget.

Independent diagnostics use original public FinanceBench PDFs and SciFact TRAIN
abstracts. Candidate processes receive only original inputs, queries and
budgets; annotation scoring runs separately. Additional report collections
contain 467 pages/six questions and 571 pages/21 questions. A SciFact sample has
120 training claims, three abstracts per claim and both 1,400/1,800-byte budgets;
every candidate is stored and each fragment is read back through the real
Memory API. A separate stress set unions multiple annotation sets for a claim;
it still has incomplete coverage and is not a reproduction of the undisclosed
official scoring selection. These developer checks are not official results.

Changed Python files pass Ruff lint/format and focused type checking. Full
pre-commit runs retain repository-wide type diagnostics; this is not reported
as a clean full-repository check. Official tests, data and dependency files
are unchanged in the submission.

Submit the repository URL, branch and complete published commit for each
selected task. Official points require the authenticated competition platform
to rerun all functional and quality gates.
