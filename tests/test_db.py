from pagecapture.db import Database


async def _make_job(db: Database, name: str = "batch") -> dict:
    return await db.create_job(
        name=name,
        output_dir="/output",
        options={"stable_ms": 5000},
        items=[
            ("https://a.example/", "desktop"),
            ("https://b.example/", "desktop"),
        ],
    )


async def test_create_job_queues_items(db: Database):
    job = await _make_job(db)
    assert job["status"] == "queued"
    assert job["total"] == 2
    items = await db.list_items(job["id"])
    assert [item["url"] for item in items] == ["https://a.example/", "https://b.example/"]
    assert all(item["status"] == "queued" for item in items)


async def test_claim_and_finish_marks_complete(db: Database):
    job = await _make_job(db)
    first = await db.claim_queued_item()
    assert first is not None
    await db.finish_item(
        first["id"],
        first["job_id"],
        {
            "status": "complete",
            "reason": None,
            "saved": 1,
            "screenshot_path": "/output/a.png",
            "duration_ms": 1000,
        },
    )
    updated = await db.get_job(job["id"])
    assert updated["status"] == "running"
    assert updated["done"] == 1

    second = await db.claim_queued_item()
    await db.finish_item(
        second["id"],
        second["job_id"],
        {"status": "failed", "reason": "crash", "saved": 0, "error": "boom"},
    )
    done = await db.get_job(job["id"])
    assert done["status"] == "completed"
    assert done["failed"] == 1
    assert done["done"] == 2


async def test_clone_jobs_leaves_original_alone(db: Database):
    original = await _make_job(db, name="once")
    item = await db.claim_queued_item()
    await db.finish_item(
        item["id"],
        item["job_id"],
        {"status": "complete", "saved": 1, "screenshot_path": "/output/a.png"},
    )
    result = await db.clone_jobs([original["id"]])
    assert len(result["created"]) == 1
    clone = result["created"][0]
    assert clone["id"] != original["id"]
    assert clone["name"] == "once"
    assert clone["status"] == "queued"
    assert clone["total"] == 2
    orig_items = await db.list_items(original["id"])
    assert any(row["saved"] == 1 for row in orig_items)
    clone_items = await db.list_items(clone["id"])
    assert all(row["status"] == "queued" and row["saved"] == 0 for row in clone_items)


async def test_retry_unsaved_only_requeues_items_without_files(db: Database):
    job = await _make_job(db)
    first = await db.claim_queued_item()
    await db.finish_item(
        first["id"],
        first["job_id"],
        {"status": "complete", "saved": 1, "screenshot_path": "/output/a.png"},
    )
    second = await db.claim_queued_item()
    await db.finish_item(
        second["id"],
        second["job_id"],
        {"status": "failed", "reason": "crash", "saved": 0, "error": "nav"},
    )
    result = await db.retry_unsaved([job["id"]])
    assert result["retried"][0]["items"] == 1
    items = {row["id"]: row for row in await db.list_items(job["id"])}
    assert items[first["id"]]["status"] == "complete"
    assert items[first["id"]]["screenshot_path"] == "/output/a.png"
    assert items[second["id"]]["status"] == "queued"
    assert items[second["id"]]["error"] is None
    refreshed = await db.get_job(job["id"])
    assert refreshed["status"] == "queued"
    assert refreshed["failed"] == 0


async def test_retry_unsaved_skips_when_nothing_failed(db: Database):
    job = await _make_job(db)
    result = await db.retry_unsaved([job["id"]])
    assert result["retried"] == []
    assert result["skipped"][0]["reason"] == "nothing to retry"


async def test_reset_running_puts_items_back_on_queue(db: Database):
    await _make_job(db)
    claimed = await db.claim_queued_item()
    assert claimed["status"] == "running"
    await db.reset_running()
    job = await db.get_job(claimed["job_id"])
    item = await db.get_item(claimed["job_id"], claimed["id"])
    assert job["status"] == "queued"
    assert item["status"] == "queued"
