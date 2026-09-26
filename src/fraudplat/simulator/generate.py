"""Transaction generation. Deterministic for a given config (including seed) and library versions.

Independent random streams are spawned from the seed for profiles, transactions, fraud, arrival
delay and label delay, so changing e.g. the arrival parameters does not change the transactions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import polars as pl

from fraudplat.features.spec import DAY, US
from fraudplat.simulator.config import SimulatorConfig

GENERATOR_VERSION = "sim-1"


@dataclass(frozen=True)
class SimulatedDataset:
    customers: pl.DataFrame
    terminals: pl.DataFrame
    transactions: pl.DataFrame


def _truncated_normal(
    rng: np.random.Generator, mean: float, std: float, size: int, low: float, high: float
) -> np.ndarray:
    """Normal draws restricted to (low, high) by rejection: our choice where the prose is silent."""
    out = rng.normal(mean, std, size)
    bad = (out <= low) | (out >= high)
    while bad.any():
        out[bad] = rng.normal(mean, std, int(bad.sum()))
        bad = (out <= low) | (out >= high)
    return out


def generate(config: SimulatorConfig) -> SimulatedDataset:
    profile_rng, tx_rng, fraud_rng, arrival_rng, label_rng = (
        np.random.default_rng(s) for s in np.random.SeedSequence(config.seed).spawn(5)
    )

    # Customer and terminal profiles (handbook prose: 100x100 grid, mean amount U(5, 100),
    # std = mean / 2, transactions per day ~ Poisson(U(0, 4))).
    n_c, n_t = config.n_customers, config.n_terminals
    cust_xy = profile_rng.uniform(0, 100, (n_c, 2))
    mean_amount = profile_rng.uniform(5, 100, n_c)
    std_amount = mean_amount / 2
    tx_per_day = profile_rng.uniform(0, 4, n_c)
    term_xy = profile_rng.uniform(0, 100, (n_t, 2))

    available: list[np.ndarray] = []
    for i in range(n_c):
        dist = np.sqrt(((term_xy - cust_xy[i]) ** 2).sum(axis=1))
        available.append(np.flatnonzero(dist < config.radius))

    # Transactions.
    start_us = int(datetime.combine(config.start_date, datetime.min.time(), UTC).timestamp()) * US
    cust_idx: list[np.ndarray] = []
    term_idx: list[np.ndarray] = []
    amounts: list[np.ndarray] = []
    times_us: list[np.ndarray] = []
    for i in range(n_c):
        per_day = tx_rng.poisson(tx_per_day[i], config.n_days)
        total = int(per_day.sum())
        if total == 0 or available[i].size == 0:
            continue  # customers with no terminal in radius transact nowhere (our choice)
        days = np.repeat(np.arange(config.n_days), per_day)
        seconds = np.floor(
            _truncated_normal(
                tx_rng, config.time_of_day_mean_s, config.time_of_day_std_s, total, 0, 86_400
            )
        ).astype(np.int64)
        amount = _truncated_normal(tx_rng, mean_amount[i], std_amount[i], total, 0, np.inf)
        cust_idx.append(np.full(total, i))
        term_idx.append(tx_rng.choice(available[i], total))
        amounts.append(np.maximum(np.round(amount * 100), 1).astype(np.int64))
        times_us.append(start_us + days.astype(np.int64) * DAY + seconds * US)

    c = np.concatenate(cust_idx)
    t = np.concatenate(term_idx)
    amount_minor = np.concatenate(amounts)
    event_us = np.concatenate(times_us)
    order = np.lexsort((t, c, event_us))  # by event time, then customer, then terminal
    c, t, amount_minor, event_us = c[order], t[order], amount_minor[order], event_us[order]
    n = c.size
    day_of = (event_us - start_us) // DAY

    # Fraud scenarios, in the order the handbook lists them.
    sc = config.scenarios
    fraud_s1 = amount_minor > sc.s1_amount_threshold_minor
    fraud_s2 = np.zeros(n, dtype=bool)
    fraud_s3 = np.zeros(n, dtype=bool)
    for day in range(config.n_days):
        window = (day_of >= day) & (day_of < day + sc.s2_duration_days)
        compromised = fraud_rng.choice(n_t, sc.s2_terminals_per_day, replace=False)
        fraud_s2 |= window & np.isin(t, compromised)
    for day in range(config.n_days):
        window = (day_of >= day) & (day_of < day + sc.s3_duration_days)
        leaked = fraud_rng.choice(n_c, sc.s3_customers_per_day, replace=False)
        candidates = np.flatnonzero(window & np.isin(c, leaked) & ~fraud_s3)
        chosen = candidates[fraud_rng.random(candidates.size) < sc.s3_fraction]
        amount_minor[chosen] *= sc.s3_multiplier
        fraud_s3[chosen] = True
    is_fraud = fraud_s1 | fraud_s2 | fraud_s3

    # Synthetic arrival delay: mostly sub-second, a small fraction delayed minutes to hours.
    ar = config.arrival
    delayed = arrival_rng.random(n) < ar.delayed_fraction
    typical_us = arrival_rng.lognormal(np.log(ar.typical_median_ms * 1000), ar.typical_sigma, n)
    late_us = arrival_rng.uniform(ar.delayed_min_s * US, ar.delayed_max_s * US, n)
    received_us = event_us + np.where(delayed, late_us, typical_us).astype(np.int64)

    # Synthetic label availability.
    lb = config.labels
    fraud_delay = label_rng.uniform(lb.fraud_delay_min_days * DAY, lb.fraud_delay_max_days * DAY, n)
    label_delay = np.where(is_fraud, fraud_delay, lb.legit_maturity_days * DAY).astype(np.int64)

    def ts(values: np.ndarray) -> pl.Series:
        return pl.Series(values).cast(pl.Datetime("us", "UTC"))

    transactions = pl.DataFrame(
        {
            "transaction_id": [f"TX{k:09d}" for k in range(n)],
            "customer_id": [f"C{k:06d}" for k in c],
            "terminal_id": [f"T{k:06d}" for k in t],
            "amount_minor": amount_minor,
            "currency": config.currency,
            "event_time": ts(event_us),
            "received_at": ts(received_us),
            "label_available_at": ts(event_us + label_delay),
            "is_fraud": is_fraud,
            "fraud_s1": fraud_s1,
            "fraud_s2": fraud_s2,
            "fraud_s3": fraud_s3,
            "arrival_delayed": delayed,
        }
    )
    customers = pl.DataFrame(
        {
            "customer_id": [f"C{k:06d}" for k in range(n_c)],
            "x": cust_xy[:, 0],
            "y": cust_xy[:, 1],
            "mean_amount": mean_amount,
            "std_amount": std_amount,
            "mean_tx_per_day": tx_per_day,
            "available_terminal_count": [a.size for a in available],
        }
    )
    terminals = pl.DataFrame(
        {"terminal_id": [f"T{k:06d}" for k in range(n_t)], "x": term_xy[:, 0], "y": term_xy[:, 1]}
    )
    return SimulatedDataset(customers=customers, terminals=terminals, transactions=transactions)
