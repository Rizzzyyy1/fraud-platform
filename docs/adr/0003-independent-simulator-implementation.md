# ADR 0003 — Independent simulator implementation

**Status:** accepted

**Context.** The Fraud Detection Handbook's simulator is the preferred data source. Its README
states that the notebook code is released under GPL-3.0 and the prose under CC BY-SA 4.0. The
project also needs additions the original does not describe: arrival delay, label availability,
integer minor-unit amounts, and immutable versioned output.

**Decision.** Implement the generative process from the book's prose description, without
copying or translating handbook code, so the repository does not reuse that code. Cite the
book, list the source material consulted, and record every parameter the prose leaves open as
this project's choice (docs/DATASET_CARD.md).

**Consequences.** These datasets are not byte-compatible with the handbook's published data, and
results must not be compared with its reported numbers as if they were the same data. Scenarios
can be extended without depending on the original code.

**Note (v1.0.0).** The implementation adapts the book's described design and example parameter
values with attribution; the exact provenance (what was consulted, adapted and written
independently) is in `THIRD_PARTY_NOTICES.md`. That record is a factual account, not a legal
opinion.
