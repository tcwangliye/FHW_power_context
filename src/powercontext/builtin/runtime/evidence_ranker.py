"""Generic sentence relevance fitted on public SciFact TRAIN features."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable

_COEFFICIENTS = (
    -10.169652870937416,
    -1.9747168196795986,
    1.1259891897460017,
    3.6289564212004723,
    0.0971616525984866,
    -0.5936925051577462,
    0.18809399774729477,
    0.0975682891111158,
    -1.4692384804994605,
    3.859975411342449,
    1.5536587723590747,
    0.848442047658697,
    0.6847558793756509,
    1.8212144221743216,
    -1.9563149759131486,
    -0.6022080862928217,
    0.08760317178457061,
    0.16669694427659967,
    -0.5276071120354893,
    -2.182978678087712,
    -2.4918486058875557,
    0.3200781632352525,
    -0.6796027706541027,
    0.1902830941368775,
    0.3422682067020644,
    0.1594581954930478,
    -0.18389306728739382,
    -0.9564219750540743,
    -0.010842683973172009,
    4.1663023702095,
)


class SentenceEvidenceRanker:
    def __init__(self, query, documents, spans, tokenize: Callable[[str], list[str]]):
        self.query = query
        self.documents = documents
        self.spans = spans
        self.tokenize = tokenize
        counts = []
        for text, sentences in zip(documents, spans, strict=True):
            unique = {" ".join(text[start:end].casefold().split()): text[start:end] for start, end in sentences}
            counts.append(Counter(term for sentence in unique.values() for term in tokenize(sentence)))
        frequency = Counter(term for count in counts for term in count)
        self.weights = {
            term: math.log(1 + (len(documents) + 0.5) / (frequency[term] + 0.5))
            * (3 if any(character.isdigit() for character in term) and not term.isdigit() else 1)
            for term in query
        }
        self.total = sum(self.weights.values()) or 1
        self.identifiers = {
            term for term in query if any(character.isdigit() for character in term) and not term.isdigit()
        }
        self.doc_coverage = [self._coverage(count) for count in counts]
        self.doc_scores = [
            sum(self.weights[term] * count[term] * 2 / (count[term] + 1) for term in query) / self.total
            for count in counts
        ]
        self.titles = [self._coverage(Counter(tokenize(text[:250].split(". ")[0]))) for text in documents]
        self.best_doc = max(self.doc_scores, default=1) or 1
        self.overlaps = [
            [self._coverage(Counter(tokenize(text[start:end]))) for start, end in sentences]
            for text, sentences in zip(documents, spans, strict=True)
        ]

    def _coverage(self, counts):
        return sum(self.weights[term] for term in self.query if counts[term]) / self.total

    def score(self, document_index: int, sentence_index: int, start: int, end: int) -> float:
        values = self._features(document_index, sentence_index, start, end)
        prediction = sum(value * coefficient for value, coefficient in zip(values, _COEFFICIENTS, strict=True))
        probability = 1 / (1 + math.exp(-max(-30, min(prediction, 30))))
        return probability * len(self.documents[document_index][start:end].split())

    def _features(self, document_index: int, index: int, start: int, end: int):
        text = self.documents[document_index]
        body = text[start:end]
        lower = body.lower()
        count = Counter(self.tokenize(body))
        length = sum(count.values())
        labels = re.findall(
            r"\b(BACKGROUND|IMPORTANCE|OBJECTIVES?|PURPOSE|METHODS?|DESIGN|RESULTS?|FINDINGS|CONCLUSIONS?|INTERPRETATION)\b",
            text[:end],
        )
        label = labels[-1] if labels else ""
        matched = self.query & count.keys()
        coverage = self._coverage(count)
        overlaps = self.overlaps[document_index]
        relative_document = self.doc_scores[document_index] / self.best_doc
        return (
            1.0,
            coverage,
            len(matched) / max(len(self.query), 1),
            len(matched) / max(length, 1),
            sum(self.weights[term] * min(count[term], 3) for term in matched) / self.total,
            sum(self.weights[term] * count[term] for term in matched) / (self.total * math.sqrt(max(length, 1))),
            len(self.identifiers & count.keys()) / max(len(self.identifiers), 1),
            float(bool(self.identifiers) and not self.identifiers.intersection(count)),
            self.doc_coverage[document_index],
            relative_document,
            self.titles[document_index],
            coverage / (max(overlaps, default=1) or 1),
            math.log1p(length),
            index / max(len(overlaps) - 1, 1),
            float(index == 0),
            float(index == len(overlaps) - 1),
            float(label.startswith(("RESULT", "FINDING"))),
            float(label.startswith(("CONCLUSION", "INTERPRETATION"))),
            float(label in ("BACKGROUND", "IMPORTANCE")),
            float(label.startswith(("METHOD", "DESIGN"))),
            float(label.startswith(("OBJECTIVE", "PURPOSE"))),
            float(
                bool(
                    re.search(
                        r"\b(?:found|show(?:ed)?|demonstrat\w*|observed|associated|correlated|increased|decreased|reduced|higher|lower|reveal\w*|resulted|indicat\w*)\b",
                        lower,
                    )
                )
            ),
            float(
                bool(
                    re.search(
                        r"\b(?:we (?:conclude|propose|suggest)|taken together|in (?:summary|conclusion)|these (?:findings|results)|our (?:results|findings))\b",
                        lower,
                    )
                )
            ),
            float(bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|fold)|\b(?:mean|ratio|risk|rate)\b.*\d", lower))),
            float(bool(re.search(r"\b(?:not|no|without|failed|lack|unchanged)\b", lower))),
            float(bool(re.search(r"\b[pP]\s*[<=>]|confidence interval|\bCI\b", body))),
            overlaps[index - 1] if index else 0,
            overlaps[index + 1] if index + 1 < len(overlaps) else 0,
            math.log1p(len(self.query)),
            coverage * relative_document,
        )
