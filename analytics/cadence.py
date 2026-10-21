"""Collection-cadence monitoring: is daily collection worth it, or would weekly do.

Each scheduled run appends one observation (which postings were seen, how long the run
took, how big the data is) to ``gold/cadence/observations.jsonl``. The report replays
the daily observations as if collection had run every 7 or 14 days instead and measures
what that would have cost:

* **missed postings** - postings that appeared and disappeared between two slower runs,
  so the slower schedule never saw them at all;
* **discovery delay** - how many days later the slower schedule first saw a posting;
* **removal delay** - how many days longer a removed posting would still look open.

The recommendation is the slowest cadence whose missed share stays under a threshold,
and it is only made once enough daily history exists to simulate the slower schedules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

from models.lifecycle import LifecycleState

OBSERVATIONS_FILE = Path("cadence") / "observations.jsonl"
CADENCES = {"daily": 1, "weekly": 7, "biweekly": 14}
# Runs drift by minutes or hours; a run counts for a slot when it is at least this
# close to the slot's nominal spacing.
_SLACK = timedelta(hours=12)


@dataclass(frozen=True)
class Observation:
    """One scheduled collection run: when it happened and which postings it saw."""

    run_id: str
    observed_at: datetime
    job_ids: frozenset[str]
    scheduled: bool = True
    run_seconds: float | None = None
    data_bytes: int | None = None

    def to_json(self) -> str:
        """Serialize as one JSON line, job IDs sorted for stable diffs."""
        return json.dumps(
            {
                "run_id": self.run_id,
                "observed_at": self.observed_at.isoformat(),
                "scheduled": self.scheduled,
                "run_seconds": self.run_seconds,
                "data_bytes": self.data_bytes,
                "job_ids": sorted(self.job_ids),
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, line: str) -> Observation:
        """Parse one JSON line written by ``to_json``."""
        payload = json.loads(line)
        return cls(
            run_id=str(payload["run_id"]),
            observed_at=datetime.fromisoformat(payload["observed_at"]),
            job_ids=frozenset(str(job_id) for job_id in payload["job_ids"]),
            scheduled=bool(payload.get("scheduled", True)),
            run_seconds=payload.get("run_seconds"),
            data_bytes=payload.get("data_bytes"),
        )


def observation_from_state(
    state: LifecycleState,
    *,
    scheduled: bool,
    run_seconds: float | None = None,
    data_bytes: int | None = None,
) -> Observation:
    """Return the latest run's observation: postings whose last sighting is that run."""
    if state.last_collection_run_id is None:
        raise ValueError("lifecycle state has no collection run yet")
    observed_at = state.updated_at
    seen = frozenset(
        entry.job_id for entry in state.entries.values() if entry.last_seen_at == observed_at
    )
    return Observation(
        run_id=state.last_collection_run_id,
        observed_at=observed_at,
        job_ids=seen,
        scheduled=scheduled,
        run_seconds=run_seconds,
        data_bytes=data_bytes,
    )


def load_observations(path: Path) -> list[Observation]:
    """Read the observation log, oldest first, keeping one entry per run."""
    if not path.exists():
        return []
    by_run: dict[str, Observation] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            observation = Observation.from_json(line)
            by_run[observation.run_id] = observation
    return sorted(by_run.values(), key=lambda item: item.observed_at)


def append_observation(path: Path, observation: Observation) -> bool:
    """Append an observation unless that run is already logged; return True if added."""
    if any(item.run_id == observation.run_id for item in load_observations(path)):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(observation.to_json() + "\n")
    return True


def sample_runs(runs: list[Observation], every_days: int) -> list[Observation]:
    """Pick the runs a schedule of one run every ``every_days`` days would have made."""
    chosen: list[Observation] = []
    spacing = timedelta(days=every_days)
    for run in runs:
        if not chosen or run.observed_at - chosen[-1].observed_at >= spacing - _SLACK:
            chosen.append(run)
    return chosen


def _days(value: timedelta) -> float:
    return round(value.total_seconds() / 86_400, 2)


def _percentile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))
    return round(ordered[index], 2)


def simulate(runs: list[Observation], every_days: int) -> dict[str, object]:
    """Compare a slower schedule against the full daily history."""
    sampled = sample_runs(runs, every_days)
    first_seen: dict[str, datetime] = {}
    last_seen: dict[str, datetime] = {}
    for run in runs:
        for job_id in run.job_ids:
            first_seen.setdefault(job_id, run.observed_at)
            last_seen[job_id] = run.observed_at
    sampled_first: dict[str, datetime] = {}
    for run in sampled:
        for job_id in run.job_ids:
            sampled_first.setdefault(job_id, run.observed_at)
    missed = sorted(job_id for job_id in first_seen if job_id not in sampled_first)
    discovery = [_days(sampled_first[job_id] - first_seen[job_id]) for job_id in sampled_first]
    final_run = runs[-1].observed_at if runs else None
    removal: list[float] = []
    for job_id, last in last_seen.items():
        if last == final_run or job_id not in sampled_first:
            continue  # still open, or never seen by the slower schedule
        later = next((run for run in sampled if run.observed_at > last), None)
        if later is not None:
            removal.append(_days(later.observed_at - last))
    total = len(first_seen)
    return {
        "every_days": every_days,
        "runs": len(sampled),
        "postings_seen": len(sampled_first),
        "missed_postings": len(missed),
        "missed_share": round(len(missed) / total, 4) if total else 0.0,
        "discovery_delay_days": {
            "median": round(median(discovery), 2) if discovery else None,
            "p90": _percentile(discovery, 0.9),
        },
        "removal_delay_days": {
            "median": round(median(removal), 2) if removal else None,
            "p90": _percentile(removal, 0.9),
        },
        "missed_examples": missed[:10],
    }


def cadence_report(
    observations: list[Observation],
    *,
    minimum_days: int = 28,
    max_missed_share: float = 0.05,
    now: datetime | None = None,
) -> dict[str, object]:
    """Build the cadence report from scheduled observations."""
    runs = [item for item in observations if item.scheduled]
    generated_at = now or datetime.now(UTC)
    if not runs:
        return {
            "generated_at": generated_at.isoformat(),
            "status": "no_data",
            "message": "No scheduled collection runs are logged yet.",
        }
    span_days = _days(runs[-1].observed_at - runs[0].observed_at)
    all_ids = {job_id for run in runs for job_id in run.job_ids}
    daily_new = [
        len(current.job_ids - previous.job_ids)
        for previous, current in zip(runs, runs[1:], strict=False)
    ]
    daily_gone = [
        len(previous.job_ids - current.job_ids)
        for previous, current in zip(runs, runs[1:], strict=False)
    ]
    seconds = [run.run_seconds for run in runs if run.run_seconds is not None]
    sizes = [run.data_bytes for run in runs if run.data_bytes is not None]
    schedules = {name: simulate(runs, days) for name, days in CADENCES.items()}
    ready = span_days >= minimum_days
    recommendation: str | None = None
    reason: str
    if ready:
        eligible = [
            name
            for name, days in CADENCES.items()
            if float(str(schedules[name]["missed_share"])) <= max_missed_share
        ]
        recommendation = max(eligible, key=lambda name: CADENCES[name])
        reason = (
            f"Slowest schedule that misses at most {max_missed_share:.0%} of postings "
            f"seen by daily runs over {span_days:g} days."
        )
    else:
        reason = (
            f"Keep daily runs: {span_days:g} of {minimum_days} days of history so far, "
            "too little to judge weekly or biweekly schedules."
        )
    minutes_per_run = round(median(seconds) / 60, 1) if seconds else None
    return {
        "generated_at": generated_at.isoformat(),
        "status": "ready" if ready else "collecting",
        "history": {
            "scheduled_runs": len(runs),
            "first_run_at": runs[0].observed_at.isoformat(),
            "last_run_at": runs[-1].observed_at.isoformat(),
            "span_days": span_days,
            "distinct_postings": len(all_ids),
            "postings_last_run": len(runs[-1].job_ids),
        },
        "churn_per_run": {
            "new_median": median(daily_new) if daily_new else None,
            "gone_median": median(daily_gone) if daily_gone else None,
        },
        "cost": {
            "median_run_minutes": minutes_per_run,
            "actions_minutes_per_month": {
                name: round(minutes_per_run * 30 / days, 1) if minutes_per_run else None
                for name, days in CADENCES.items()
            },
            "data_megabytes": round(sizes[-1] / 1_000_000, 1) if sizes else None,
            "data_growth_megabytes_per_run": (
                round((sizes[-1] - sizes[0]) / 1_000_000 / (len(sizes) - 1), 2)
                if len(sizes) > 1
                else None
            ),
        },
        "schedules": schedules,
        "recommendation": recommendation,
        "reason": reason,
        "thresholds": {"minimum_days": minimum_days, "max_missed_share": max_missed_share},
    }


def _show(value: object) -> str:
    return "–" if value is None else str(value)


def report_markdown(report: dict[str, object]) -> str:
    """Render the report as a short Markdown summary (workflow summary, data repo)."""
    if report.get("status") == "no_data":
        return f"## Collection cadence\n\n{report['message']}\n"
    history = report["history"]
    cost = report["cost"]
    schedules = report["schedules"]
    assert isinstance(history, dict) and isinstance(cost, dict) and isinstance(schedules, dict)
    minutes = cost["actions_minutes_per_month"]
    assert isinstance(minutes, dict)
    lines = [
        "## Collection cadence",
        "",
        f"**{report['recommendation'] or 'daily (still collecting evidence)'}** - "
        f"{report['reason']}",
        "",
        f"{history['scheduled_runs']} scheduled runs over {history['span_days']:g} days, "
        f"{history['distinct_postings']} distinct postings "
        f"({history['postings_last_run']} in the latest run).",
        "",
        "| Schedule | Runs | Missed postings | Discovery delay (median / p90 days) "
        "| Removal delay (median days) | Actions min/month |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, result in schedules.items():
        assert isinstance(result, dict)
        discovery = result["discovery_delay_days"]
        removal = result["removal_delay_days"]
        assert isinstance(discovery, dict) and isinstance(removal, dict)
        lines.append(
            f"| {name} | {result['runs']} | {result['missed_postings']} "
            f"({float(str(result['missed_share'])):.1%}) | {_show(discovery['median'])} / "
            f"{_show(discovery['p90'])} | {_show(removal['median'])} | "
            f"{_show(minutes.get(name))} |"
        )
    lines += [
        "",
        f"Median run: {_show(cost['median_run_minutes'])} min. "
        f"Data: {_show(cost['data_megabytes'])} MB "
        f"(+{_show(cost['data_growth_megabytes_per_run'])} MB per run).",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "CADENCES",
    "OBSERVATIONS_FILE",
    "Observation",
    "append_observation",
    "cadence_report",
    "load_observations",
    "observation_from_state",
    "report_markdown",
    "sample_runs",
    "simulate",
]
