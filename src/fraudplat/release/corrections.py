"""Reporting corrections for release-1, derived only from the frozen evaluation JSON.

Nothing is recomputed from data: the held-out evaluation stays as run. The baseline's
"flagged" set combines reviews and automatic declines; the frozen report labelled its legitimate
count as "sent to review". This splits it exactly using the stored counts and decline precision:

    declined_fraud      = round(decline_precision * declined)   (checked to be exact)
    declined_legitimate = declined - declined_fraud
    reviewed            = flagged - declined
    legitimate_reviewed = legitimate_flagged - declined_legitimate

Run: `python -m fraudplat.release.corrections --release release-1`
"""

# ruff: noqa: E501  -- explanatory strings in the corrections record

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def split(model: dict[str, Any]) -> dict[str, Any]:
    o = model["overall"]
    legit_flagged = o["legitimate_sent_to_review"]  # frozen field name; it counts all flagged
    out: dict[str, Any] = {
        "flagged": o["flagged"],
        "flagged_rate": o["flagged_rate"],
        "legitimate_flagged": legit_flagged,
    }
    declined = model.get("declined")
    if declined is None:
        out.update(
            reviewed=o["flagged"],
            review_rate=o["flagged_rate"],
            declined=0,
            legitimate_reviewed=legit_flagged,
            legitimate_declined=0,
        )
        return out
    exact = model["decline_precision"] * declined
    declined_fraud = round(exact)
    if abs(exact - declined_fraud) > 1e-6:
        raise ValueError("stored decline precision does not give an integer count")
    legit_declined = declined - declined_fraud
    reviewed = o["flagged"] - declined
    out.update(
        reviewed=reviewed,
        review_rate=reviewed / o["rows"],
        declined=declined,
        decline_rate=declined / o["rows"],
        declined_fraud=declined_fraud,
        legitimate_declined=legit_declined,
        legitimate_reviewed=legit_flagged - legit_declined,
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", default="release-1")
    args = parser.parse_args()
    base = Path("reports") / args.release
    frozen = json.loads((base / "test_evaluation.json").read_text())
    corrections: dict[str, Any] = {
        "source": "test_evaluation.json (unchanged); values derived arithmetically, no data reread",
        "terminology": {
            "flagged": "reviewed or automatically declined",
            "reviewed": "sent to analyst review (not declined)",
            "note": "the frozen field 'legitimate_sent_to_review' counts legitimate transactions that "
            "were flagged (reviewed or declined); for the review-only candidate the two coincide",
        },
        "conditions": {},
        "denominator": {
            "evaluated_rows": frozen["conditions"]["A_immediate_processing"]["test_rows"],
            "delay_check_rows": 301_010,
            "difference": 301_010 - frozen["conditions"]["A_immediate_processing"]["test_rows"],
            "explanation": (
                "The evaluation window [153, 183) in decision time was fixed in the release manifest "
                "before the test was read. Nine transactions happened on day 182 but arrived (and so "
                "were decided) on day 183, after the simulated period; the window excludes them. The "
                "follow-up delay count used day >= 153 with no upper bound and so included them. The "
                "window itself was predefined; the existence of these nine late arrivals was not "
                "specifically anticipated."
            ),
        },
    }
    for cond, c in frozen["conditions"].items():
        corrections["conditions"][cond] = {
            "candidate": split(c["candidate"]),
            "baseline": split(c["baseline"]),
        }
    (base / "test_evaluation_corrections.json").write_text(json.dumps(corrections, indent=2) + "\n")
    print(json.dumps(corrections["conditions"]["A_immediate_processing"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
