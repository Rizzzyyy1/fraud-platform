"""Deterministic payment-transaction simulator.

An independent implementation of the generative process described in the prose of
Le Borgne, Siblini, Lebichot and Bontempi, *Reproducible Machine Learning for Credit Card
Fraud Detection — Practical Handbook* (2022), https://github.com/Fraud-Detection-Handbook/.
No code from the handbook (GPL-3.0) is used. Parameters the prose leaves open, and the
additions (arrival delay, label delay), are this project's choices; see docs/DATASET_CARD.md.
"""
