# pyright: reportMissingImports=false
from pathlib import Path

from app.db import Database


def make_db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.sqlite3")
    database.initialize(1)
    database.add_repository(
        github_repo_id=10,
        installation_id=20,
        full_name="owner/repo",
        default_branch="main",
        verification_command="pytest",
    )
    return database


def test_delivery_deduplication_and_atomic_claim(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    assert database.record_delivery("delivery-1", "issues", {"action": "opened"})
    assert not database.record_delivery("delivery-1", "issues", {"action": "opened"})

    run_id = database.create_run(
        repository_id=1,
        source="manual",
        kind="advisory",
        instruction="Review issue",
    )
    assert run_id
    claimed = database.claim_run("worker-1")
    assert claimed and claimed["id"] == run_id
    assert database.claim_run("worker-2") is None


def test_delivery_and_matching_runs_commit_atomically(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    spec = {
        "repository_id": 1,
        "rule_id": None,
        "kind": "advisory",
        "instruction": "Review",
        "context": {"issue": 3},
    }
    inserted, run_ids = database.enqueue_delivery(
        "delivery-2", "issues", {"action": "opened"}, "matched", [spec, spec]
    )
    assert inserted and len(run_ids) == 2
    duplicate, duplicate_runs = database.enqueue_delivery(
        "delivery-2", "issues", {"action": "opened"}, "matched", [spec]
    )
    assert not duplicate and not duplicate_runs
    assert len(database.list_runs()) == 2


def test_only_one_retry_can_use_publication_key(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    original_id = database.create_run(
        repository_id=1, source="manual", kind="advisory", instruction="Review"
    )
    assert original_id
    original = database.get_run(original_id)
    assert original
    database.update_run(original_id, status="failed")
    first_retry = database.create_run(
        repository_id=1,
        source="manual",
        kind="advisory",
        instruction="Review",
        publication_key=original["publication_key"],
    )
    second_retry = database.create_run(
        repository_id=1,
        source="manual",
        kind="advisory",
        instruction="Review",
        publication_key=original["publication_key"],
    )
    assert first_retry and second_retry is None


def test_cancel_and_publish_transition_are_atomic(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    run_id = database.create_run(
        repository_id=1, source="manual", kind="advisory", instruction="Review"
    )
    assert run_id
    database.update_run(run_id, status="running")
    assert database.request_cancel(run_id) == "running"
    assert not database.begin_publishing(run_id, "running")


def test_claim_refuses_second_active_run(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    first = database.create_run(
        repository_id=1, source="manual", kind="advisory", instruction="First"
    )
    second = database.create_run(
        repository_id=1, source="manual", kind="advisory", instruction="Second"
    )
    assert first and second and database.claim_run("worker")
    assert database.claim_run("other-scheduler") is None


def test_recovery_uses_latest_verified_publication(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    original = database.create_run(
        repository_id=1, source="manual", kind="change", instruction="Fix"
    )
    assert original
    original_run = database.get_run(original)
    assert original_run
    publication_key = original_run["publication_key"]
    database.update_run(
        original,
        status="failed",
        commit_sha="verified-sha",
        verification_output="passed",
    )
    failed_retry = database.create_run(
        repository_id=1,
        source="manual",
        kind="change",
        instruction="Fix",
        publication_key=publication_key,
    )
    assert failed_retry
    database.update_run(failed_retry, status="failed")
    current = database.create_run(
        repository_id=1,
        source="manual",
        kind="change",
        instruction="Fix",
        publication_key=publication_key,
    )
    assert current
    previous = database.previous_publication_run(publication_key, current)
    assert previous and previous["id"] == original


def test_restart_marks_inflight_run_interrupted(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    run_id = database.create_run(
        repository_id=1,
        source="manual",
        kind="change",
        instruction="Fix issue",
    )
    assert run_id
    assert database.claim_run("worker")
    assert database.recover_interrupted() == 1
    run = database.get_run(run_id)
    assert run and run["status"] == "interrupted" and run["cleanup_after"]
