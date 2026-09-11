from process_health import ProcessHealth


def test_blocked_job_is_not_reported_as_ok() -> None:
    health = ProcessHealth()
    health.record_job_ok("pm_scan")
    health.record_job_blocked("pm_scan", "unreachable")

    job = health.snapshot()["jobs"]["pm_scan"]

    assert job["state"] == "blocked"
    assert job["blocked_reason"] == "unreachable"
    assert job["last_blocked"] is not None
