# ADR 0006 — Chronological timeline and label eligibility for training

**Status:** accepted

**Context.** Fraud labels arrive late (simulated chargeback delay 1–30 days) and legitimate
labels are only trusted after 30 days. Training on everything decided before a cutoff would
include recent transactions whose labels were not yet known, and would over-represent quickly
reported fraud among the ones that were.

**Decision (sim-v2, days from 2025-01-01).**
* Days 0–29: burn-in (features lack a full 30-day history); not examples.
* Days 30–99: training examples, eligible only if `label_available_at <= training cutoff`.
* Day 130: training cutoff. Ending examples 30 days earlier means every example's label is known
  at the cutoff; the count of excluded unlabelled examples is still computed and reported (0).
* Days 130–152: validation — the decisions a model trained at the cutoff would score. Used to
  choose hyperparameters and policy thresholds; evaluated with eventual simulated labels.
* Day 153 onward: final test period. Its rows are not reconstructed, not loaded by training, and
  `split_masks` raises if any reach it.
* Preprocessing statistics come from training rows only.

**Consequences.** Validation metrics are optimistic, because validation both selects the model and
sets thresholds; a development run also led to widening the C grid after the first choice landed
on the grid edge. The untouched test period is the only unbiased estimate and will be used once.
