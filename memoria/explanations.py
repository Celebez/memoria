"""Decision / explanation logger.

Captures WHY a decision was made (rationale), what could go wrong
(risk), what could go right (reward), and how confident the actor
is. This is the 'sistem penjelasan' part of the request -- a
permanent, searchable audit trail of decisions.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import get_config
from .store import Store


@dataclass
class Explanation:
    topic: str
    decision: str
    rationale: str
    risk: str = ""
    reward: str = ""
    confidence: float = 0.5
    source: str = "agent"
    outcome: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class Explanations:
    """High-level API wrapping Store.add_explanation + markdown export."""

    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self.cfg = get_config()

    def log(self, topic: str, decision: str, rationale: str,
            risk: str = "", reward: str = "",
            confidence: float = 0.5, source: str = "agent") -> str:
        """Record a decision and write a markdown mirror for human reading."""
        exp_id = self.store.add_explanation(
            topic=topic, decision=decision, rationale=rationale,
            risk=risk, reward=reward, confidence=confidence, source=source,
        )
        self._write_markdown(exp_id, topic, decision, rationale,
                             risk, reward, confidence, source)
        return exp_id

    def _write_markdown(self, exp_id: str, topic: str, decision: str,
                        rationale: str, risk: str, reward: str,
                        confidence: float, source: str) -> None:
        path = Path(self.cfg.explanations_dir) / f"{exp_id[:12]}-{_slug(topic)}.md"
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        body = f"""# {topic}

- **id:** `{exp_id}`
- **timestamp:** {ts}
- **source:** {source}
- **confidence:** {confidence:.0%}

## Decision
{decision}

## Rationale
{rationale}

## Risk
{risk or "_not recorded_"}

## Reward
{reward or "_not recorded_"}

---
_Recorded by Memoria. Edit outcome in SQLite `explanations.outcome` to close the loop._
"""
        path.write_text(body)

    def list(self, topic: str | None = None, limit: int = 50) -> list[dict]:
        return self.store.list_explanations(topic=topic, limit=limit)

    def search(self, query: str, limit: int = 20) -> list[dict]:
        return self.store.search_explanations(query=query, limit=limit)

    def record_outcome(self, exp_id: str, outcome: str) -> bool:
        """Mark a past decision's outcome: success | partial | fail."""
        import sqlite3
        try:
            with self.store._conn() as c:  # noqa: SLF001 (intentional)
                cur = c.execute(
                    "UPDATE explanations SET outcome = ? WHERE id = ?",
                    (outcome, exp_id),
                )
                return cur.rowcount > 0
        except sqlite3.Error:
            return False

    def stats(self) -> dict:
        items = self.store.list_explanations(limit=10000)
        if not items:
            return {"total": 0}
        by_outcome: dict[str, int] = {}
        avg_conf = sum(i["confidence"] for i in items) / len(items)
        for it in items:
            by_outcome[it.get("outcome") or "pending"] = by_outcome.get(
                it.get("outcome") or "pending", 0) + 1
        return {
            "total": len(items),
            "avg_confidence": round(avg_conf, 3),
            "by_outcome": by_outcome,
        }


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower())[:50].strip("-")