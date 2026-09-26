"""Behavioural features with explicit time semantics.

`spec` defines windows, `state` holds applied events, `compute` is the pure feature function,
`offline` reconstructs point-in-time features from history. Online and offline paths share
`compute` and the state semantics; that reduces implementation skew but does not prove that both
paths saw the same history (see docs/DESIGN.md, "Parity").
"""
