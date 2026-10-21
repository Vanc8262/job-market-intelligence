"""Collection-cadence monitoring replays daily runs as weekly and biweekly schedules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from analytics.cadence import (
    Observation,
    append_observation,
    cadence_report,
    load_observations,
    observation_from_state,
    report_markdown,
    sample_runs,
    simulate,
)
from models.lifecycle import LifecycleEntry, LifecycleState

START = datetime(2026, 11, 1, 10, 17, tzinfo=UTC)


def daily_runs(days: int, short_lived_every: int | None = None) -> list[Observation]:
    """Daily runs over a stable set; optionally a 2-day posting every few days."""
    runs = []
    for day in range(days):
        ids = {f"stable-{index}" for index in range(40)}
        if short_lived_every:
            for start in range(0, days, short_lived_every):
                if start <= day < start + 2:
                    ids.add(f"brief-{start}")
        runs.append(
            Observation(
                run_id=f"run-{day}",
                observed_at=START + timedelta(days=day, minutes=day % 5),
                job_ids=frozenset(ids),
                run_seconds=300,
                data_bytes=10_000_000 + day * 200_000,
            )
        )
    return runs


def test_sampling_follows_the_schedule() -> None:
    runs = daily_runs(30)
    assert len(sample_runs(runs, 1)) == 30
    weekly = sample_runs(runs, 7)
    assert [run.run_id for run in weekly] == ["run-0", "run-7", "run-14", "run-21", "run-28"]
    assert len(sample_runs(runs, 14)) == 3


def test_simulation_counts_postings_a_slower_schedule_misses() -> None:
    runs = daily_runs(30, short_lived_every=3)

    weekly = simulate(runs, 7)
    daily = simulate(runs, 1)

    assert daily["missed_postings"] == 0
    assert weekly["missed_postings"] > 0
    assert 0 < float(str(weekly["missed_share"])) < 1
    delay = weekly["discovery_delay_days"]
    assert isinstance(delay, dict) and delay["median"] == 0.0  # stable ones seen at once


def test_recommendation_waits_for_enough_history() -> None:
    early = cadence_report(daily_runs(10))
    assert early["status"] == "collecting" and early["recommendation"] is None

    stable = cadence_report(daily_runs(30))
    assert stable["status"] == "ready" and stable["recommendation"] == "biweekly"

    churny = cadence_report(daily_runs(30, short_lived_every=3))
    assert churny["recommendation"] == "daily"
    cost = churny["cost"]
    assert isinstance(cost, dict)
    assert cost["median_run_minutes"] == 5.0
    assert cost["actions_minutes_per_month"] == {"daily": 150.0, "weekly": 21.4, "biweekly": 10.7}
    assert "| weekly |" in report_markdown(churny)


def test_manual_runs_do_not_count_and_no_data_is_reported() -> None:
    manual = Observation("manual", START, frozenset({"a"}), scheduled=False)
    assert cadence_report([manual])["status"] == "no_data"
    assert "No scheduled" in report_markdown(cadence_report([]))


def test_log_is_appended_once_per_run(tmp_path: Path) -> None:
    log = tmp_path / "cadence" / "observations.jsonl"
    first, second = daily_runs(2)

    assert append_observation(log, first)
    assert not append_observation(log, first)
    assert append_observation(log, second)

    assert load_observations(log) == [first, second]


def test_observation_comes_from_the_latest_lifecycle_run() -> None:
    def entry(job_id: str, last_seen: datetime) -> LifecycleEntry:
        return LifecycleEntry(
            target_name="target",
            source_name="greenhouse",
            source_reference=job_id,
            job_id=job_id,
            title="Data Scientist",
            employer="Example",
            profile_name="profile",
            first_seen_at=START - timedelta(days=3),
            last_seen_at=last_seen,
            last_changed_at=START - timedelta(days=3),
        )

    state = LifecycleState(
        updated_at=START,
        last_collection_run_id="run-latest",
        entries={
            "seen": entry("seen", START),
            "missing": entry("missing", START - timedelta(days=1)),
        },
    )

    observation = observation_from_state(state, scheduled=True, run_seconds=12.5)

    assert observation.run_id == "run-latest"
    assert observation.job_ids == frozenset({"seen"})
    assert observation.run_seconds == 12.5
