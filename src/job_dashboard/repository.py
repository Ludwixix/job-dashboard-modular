from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUSES = ("sourced", "shortlisted", "applied", "interviewing", "offer", "rejected")


class JobRepository:
    """SQLite persistence for jobs, Kanban status, and application events."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL,
                location TEXT, description TEXT, source TEXT, url TEXT, posted TEXT,
                remote INTEGER NOT NULL DEFAULT 0, stream TEXT, score INTEGER,
                data_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'sourced',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS application_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                from_status TEXT, to_status TEXT NOT NULL, occurred_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            );
            CREATE TABLE IF NOT EXISTS scrape_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                listings_found INTEGER NOT NULL DEFAULT 0,
                errors_json TEXT NOT NULL DEFAULT '[]'
            );
        """)
        self.connection.commit()

    def replace_jobs(self, jobs: list[dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            for job in jobs:
                self.connection.execute("""
                    INSERT INTO jobs (id,title,company,location,description,source,url,posted,remote,stream,score,data_json,created_at,updated_at)
                    VALUES (:id,:title,:company,:location,:description,:source,:url,:posted,:remote,:stream,:score,:data_json,:created_at,:updated_at)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title, company=excluded.company,
                    location=excluded.location, description=excluded.description, source=excluded.source,
                    url=excluded.url, posted=excluded.posted, remote=excluded.remote, stream=excluded.stream,
                    score=excluded.score, data_json=excluded.data_json, updated_at=excluded.updated_at
                """, {"id": job["id"], "title": job.get("title", ""), "company": job.get("company", ""), "location": job.get("location", ""), "description": job.get("description", ""), "source": job.get("source", ""), "url": job.get("url", ""), "posted": job.get("posted", ""), "remote": int(bool(job.get("remote", False))), "stream": job.get("stream", ""), "score": job.get("score", 0), "data_json": json.dumps(job, ensure_ascii=False), "created_at": now, "updated_at": now})

    def list_jobs(self, *, location="", salary_min=None, role="", source="", match_score_min=0, stream="", status="") -> list[dict[str, Any]]:
        clauses = ["score >= ?"]
        values: list[Any] = [match_score_min]
        for field, value in (("location", location), ("source", source), ("stream", stream), ("status", status)):
            if value:
                clauses.append(f"lower({field}) LIKE lower(?)")
                values.append(f"%{value}%")
        if role:
            clauses.append("(lower(title) LIKE lower(?) OR lower(description) LIKE lower(?))")
            values.extend([f"%{role}%", f"%{role}%"])
        rows = self.connection.execute(f"SELECT * FROM jobs WHERE {' AND '.join(clauses)} ORDER BY score DESC, posted DESC", values).fetchall()
        result = []
        for row in rows:
            job = json.loads(row["data_json"])
            job["status"] = row["status"]
            result.append(job)
        return result

    def update_status(self, job_id: str, status: str) -> dict[str, Any]:
        if status not in STATUSES:
            raise ValueError(f"invalid status: {status}")
        row = self.connection.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (status, now, job_id))
            self.connection.execute("INSERT INTO application_events (job_id,from_status,to_status,occurred_at) VALUES (?,?,?,?)", (job_id, row["status"], status, now))
        return {"job_id": job_id, "status": status}

    def metrics(self) -> dict[str, Any]:
        rows = self.connection.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
        counts = {status: 0 for status in STATUSES}
        counts.update({row["status"]: row["count"] for row in rows})
        total = sum(counts.values())
        return {"total": total, "by_status": counts, "events": self.connection.execute("SELECT COUNT(*) FROM application_events").fetchone()[0]}

    def log_scrape_run(
        self,
        site: str,
        started_at: str,
        finished_at: str,
        listings_found: int,
        errors: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "site": str(site).strip(),
            "started_at": str(started_at),
            "finished_at": str(finished_at),
            "listings_found": max(0, int(listings_found)),
            "errors_json": json.dumps(
                [str(error).strip() for error in (errors or []) if str(error).strip()],
                ensure_ascii=False,
            ),
        }
        with self.connection:
            cursor = self.connection.execute("""
                INSERT INTO scrape_runs (site, started_at, finished_at, listings_found, errors_json)
                VALUES (:site, :started_at, :finished_at, :listings_found, :errors_json)
            """, payload)
        return {
            "id": cursor.lastrowid,
            "site": payload["site"],
            "started_at": payload["started_at"],
            "finished_at": payload["finished_at"],
            "listings_found": payload["listings_found"],
            "errors": json.loads(payload["errors_json"]),
        }

    def get_scrape_runs(self, *, site="", limit=20) -> list[dict[str, Any]]:
        """Return the most recent scrape runs up to `limit` with optional site filtering.

        Matching is case-insensitive and substring-based. Literal `%` and `_` characters in
        `site` are escaped, and `limit` is clamped to 1+.
        """
        clauses = []
        values: list[Any] = []
        if site:
            clauses.append("lower(site) LIKE lower(?) ESCAPE '!'")
            escaped_site = str(site).replace("!", "!!").replace("%", "!%").replace("_", "!_")
            values.append(f"%{escaped_site}%")
        query = "SELECT id, site, started_at, finished_at, listings_found, errors_json FROM scrape_runs"
        if clauses:
            query += f" WHERE {' AND '.join(clauses)}"
        query += " ORDER BY started_at DESC, id DESC LIMIT ?"
        values.append(max(1, int(limit)))
        rows = self.connection.execute(query, values).fetchall()
        return [
            {
                "id": row["id"],
                "site": row["site"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "listings_found": row["listings_found"],
                "errors": json.loads(row["errors_json"]),
            }
            for row in rows
        ]

    def get_scrape_run_summary(self) -> dict[str, Any]:
        rows = self.connection.execute("""
            SELECT site, COUNT(*) AS runs, SUM(listings_found) AS listings_found,
                   SUM(CASE WHEN COALESCE(json_array_length(errors_json), 0) > 0 THEN 1 ELSE 0 END) AS errors
            FROM scrape_runs
            GROUP BY site
        """).fetchall()
        by_site = {
            row["site"]: {
                "runs": row["runs"],
                "listings_found": row["listings_found"],
                "errors": row["errors"],
            }
            for row in rows
        }
        return {
            "runs": sum(item["runs"] for item in by_site.values()),
            "listings_found": sum(item["listings_found"] for item in by_site.values()),
            "errors": sum(item["errors"] for item in by_site.values()),
            "by_site": by_site,
        }
