from job_dashboard.repository import JobRepository


def test_repository_filters_and_logs_status_transition(tmp_path):
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.replace_jobs([{
        "id": "one", "title": "Cloud Engineer", "company": "Acme",
        "location": "Melbourne", "description": "Azure role", "source": "Seek",
        "url": "https://example.test/one", "score": 88,
    }])
    assert len(repository.list_jobs(source="Seek", match_score_min=80)) == 1
    repository.update_status("one", "shortlisted")
    assert repository.metrics()["by_status"]["shortlisted"] == 1
    assert repository.metrics()["events"] == 1


def test_repository_tracks_scrape_runs_by_site(tmp_path):
    repository = JobRepository(tmp_path / "jobs.sqlite3")

    created = repository.log_scrape_run(
        "Seek",
        "2026-08-25T10:00:00+00:00",
        "2026-08-25T10:02:00+00:00",
        4,
        ["captcha encountered"],
    )
    repository.log_scrape_run(
        "LinkedIn",
        "2026-08-25T11:00:00+00:00",
        "2026-08-25T11:05:00+00:00",
        2,
    )
    repository.log_scrape_run(
        "Seek_ops",
        "2026-08-25T12:00:00+00:00",
        "2026-08-25T12:01:00+00:00",
        1,
    )
    repository.log_scrape_run(
        "SeekXops",
        "2026-08-25T12:02:00+00:00",
        "2026-08-25T12:03:00+00:00",
        1,
    )

    assert created["site"] == "Seek"
    assert created["errors"] == ["captcha encountered"]
    assert [run["site"] for run in repository.get_scrape_runs()] == ["SeekXops", "Seek_ops", "LinkedIn", "Seek"]
    assert [run["site"] for run in repository.get_scrape_runs(site="LINKEDIN")] == ["LinkedIn"]
    assert [run["site"] for run in repository.get_scrape_runs(site="Seek_")] == ["Seek_ops"]
    assert repository.get_scrape_run_summary() == {
        "runs": 4,
        "listings_found": 8,
        "errors": 1,
        "by_site": {
            "LinkedIn": {"runs": 1, "listings_found": 2, "errors": 0},
            "Seek": {"runs": 1, "listings_found": 4, "errors": 1},
            "Seek_ops": {"runs": 1, "listings_found": 1, "errors": 0},
            "SeekXops": {"runs": 1, "listings_found": 1, "errors": 0},
        },
    }
