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
revision identities. The existing trust policy and envelope separator are
preserved. Unicode line separators are escaped inside JSON and decode back to
the original text. Chinese sentence boundaries and unspaced query terms are
handled without changing source characters. Ordinary preparation is unchanged.

A small offline ranker scores generic query, document and sentence features.
Thirty scalar weights are fitted using 350 public SciFact TRAIN claims across
220 documents. There are no query/document ID mappings, stored answers, online
models or additional inference dependencies. Repeated sentences do not inflate
document relevance; scientific identifiers retain their full spelling.

Interval packing accounts for the actual serialized citation, JSON separators,
envelope and UTF-8 text costs. It can combine non-overlapping spans within and
across entries, retaining only non-dominated cost/utility plans. Highly fragmented
inputs bound interval endpoint enumeration. Oversized sentences use literal
query-centered windows with the same byte accounting.

## Verification

Run the unchanged official suites using the fixed competition dependencies:

```bash
make -C competition docker-test
```

The ten public suites pass 95 tests. Related product regression suites pass
90 tests, including multi-report company and acronym matching after restart,
statement headings versus matching narrative, duplicate-page citations, long
result sentences, disconnected evidence, Unicode JSON framing, unspaced Chinese
queries and thousands of short sentences under a shared budget.

Independent diagnostics use original public FinanceBench PDFs and SciFact TRAIN
abstracts. Candidate processes receive only original inputs, queries and
budgets; annotation scoring runs separately. Additional report collections
contain 467 pages/six questions and 571 pages/21 questions. SciFact TRAIN is
partitioned by evidence document; fitting excludes validation documents even
from distractor candidates. An independent 39-document partition has 86 requests
at 1,400/1,800-byte budgets. Every candidate is stored and each fragment is read
back through the real Memory API. A separate stress set unions multiple
annotation alternatives and remains harder than the ordinary diagnostic sample.
These developer checks are not official results.

Training-data attribution: [SciFact](https://github.com/allenai/scifact/tree/68b98a56d93e0f9da0d2aab4e6c3294699a0f72e),
Wadden et al., *Fact or Fiction: Verifying Scientific Claims* (2020).
Claims and evidence annotations are CC BY 4.0; abstracts follow S2ORC's ODC-By
1.0 terms. The product contains only generic features and fitted scalar weights,
not the training corpus or annotations. DEV/TEST labels are not used for fitting.

Changed Python files pass Ruff lint/format and focused type checking. Full
pre-commit runs retain repository-wide type diagnostics; this is not reported
as a clean full-repository check. Official tests, data and dependency files
are unchanged in the submission.

Submit the repository URL, branch and complete published commit for each
selected task. Official points require the authenticated competition platform
to rerun all functional and quality gates.
