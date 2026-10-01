"""Track PRs merged to the base branch and the outcome of the deployments they triggered.

Writes one CSV per merge day (UTC) to output/YYYY-MM-DD.csv.
"""

import argparse
import csv
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
API_URL = "https://api.github.com"
DEFAULTS = {"owner": None, "base_branch": "main", "environment": None, "workflows": []}
FAILED_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}

CSV_FIELDS = [
    "repo",
    "pr_number",
    "pr_title",
    "pr_url",
    "author",
    "created_at",
    "merged_at",
    "hours_to_merge",
    "merged_by",
    "approvers",
    "changes_requested_by",
    "head_branch",
    "labels",
    "additions",
    "deletions",
    "changed_files",
    "merge_commit_sha",
    "deployment_status",
    "deployment_environment",
    "deployment_started_at",
    "deployment_finished_at",
    "deployed_by",
    "workflow_runs",
    "workflow_run_urls",
    "failure_reason",
]


class GitHub:
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def get(self, path, params=None):
        r = self.session.get(f"{API_URL}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def paginate(self, path, params=None, key=None):
        url = f"{API_URL}{path}"
        params = {"per_page": 100, **(params or {})}
        while url:
            r = self.session.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            yield from (data[key] if key else data)
            url = r.links.get("next", {}).get("url")
            params = None  # the "next" link already carries the query string


def parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def make_repo(entry, defaults):
    repo = {**defaults, **({"name": entry} if isinstance(entry, str) else entry)}
    if "/" not in repo["name"]:
        if not repo["owner"]:
            sys.exit(f"Repo '{repo['name']}' has no owner; set defaults.owner or use owner/name")
        repo["name"] = f"{repo['owner']}/{repo['name']}"
    return repo


def load_config(path):
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    defaults = {**DEFAULTS, **(config.get("defaults") or {})}
    return [make_repo(entry, defaults) for entry in config.get("repos") or []], defaults


def merged_prs(gh, repo, base, since, until):
    pulls = gh.paginate(
        f"/repos/{repo}/pulls",
        {"state": "closed", "base": base, "sort": "updated", "direction": "desc"},
    )
    for pr in pulls:
        # Sorted by updated_at desc and merged_at <= updated_at, so nothing older can match.
        if parse_ts(pr["updated_at"]) < since:
            break
        merged_at = parse_ts(pr["merged_at"])
        if merged_at and since <= merged_at < until:
            # The list endpoint omits merged_by and diff stats.
            yield gh.get(f"/repos/{repo}/pulls/{pr['number']}")


def review_summary(gh, repo, pr_number):
    latest = {}
    for review in gh.paginate(f"/repos/{repo}/pulls/{pr_number}/reviews"):
        if review["state"] == "COMMENTED" or not review.get("user"):
            continue
        latest[review["user"]["login"]] = review["state"]
    approvers = sorted(u for u, s in latest.items() if s == "APPROVED")
    changes_requested = sorted(u for u, s in latest.items() if s == "CHANGES_REQUESTED")
    return approvers, changes_requested


def deployments_for_sha(gh, repo, sha, environment):
    params = {"sha": sha}
    if environment:
        params["environment"] = environment
    results = []
    for dep in gh.paginate(f"/repos/{repo}/deployments", params):
        statuses = gh.get(f"/repos/{repo}/deployments/{dep['id']}/statuses", {"per_page": 1})
        results.append((dep, statuses[0] if statuses else None))
    return results


def workflow_runs_for_sha(gh, repo, sha, workflows):
    runs = gh.paginate(f"/repos/{repo}/actions/runs", {"head_sha": sha, "event": "push"}, key="workflow_runs")
    return [r for r in runs if not workflows or Path(r["path"]).name in workflows]


def failure_details(gh, repo, run):
    reasons = []
    for job in gh.paginate(f"/repos/{repo}/actions/runs/{run['id']}/jobs", key="jobs"):
        if job["conclusion"] not in ("failure", "timed_out", "cancelled"):
            continue
        steps = [s["name"] for s in job.get("steps") or [] if s["conclusion"] in ("failure", "timed_out")]
        # Job IDs double as check-run IDs, whose annotations hold the error messages.
        messages = [
            a["message"].strip().splitlines()[0]
            for a in gh.get(f"/repos/{repo}/check-runs/{job['id']}/annotations")
            if a["annotation_level"] == "failure" and a.get("message", "").strip()
        ]
        location = f"{run['name']} / {job['name']}" + (f" / {', '.join(steps)}" if steps else "")
        reasons.append(f"{location} [{job['conclusion']}]" + (f": {'; '.join(messages)}" if messages else ""))
    return reasons


def deployment_info(gh, repo, sha, environment, workflows):
    info = {
        "deployment_status": "no_deployment_found",
        "deployment_environment": "",
        "deployment_started_at": "",
        "deployment_finished_at": "",
        "deployed_by": "",
        "workflow_runs": "",
        "workflow_run_urls": "",
        "failure_reason": "",
    }
    reasons = []

    deployments = deployments_for_sha(gh, repo, sha, environment)
    if deployments:
        dep, status = max(deployments, key=lambda d: d[0]["created_at"])
        info.update(
            deployment_status=status["state"] if status else "pending",
            deployment_environment=dep["environment"],
            deployment_started_at=dep["created_at"],
            deployment_finished_at=status["created_at"] if status else "",
            deployed_by=(dep.get("creator") or {}).get("login", ""),
        )
        if status and status["state"] in ("failure", "error") and status.get("description"):
            reasons.append(f"deployment: {status['description']}")

    runs = workflow_runs_for_sha(gh, repo, sha, workflows)
    if runs:
        info["workflow_runs"] = "; ".join(f"{r['name']}:{r['conclusion'] or r['status']}" for r in runs)
        info["workflow_run_urls"] = " ".join(r["html_url"] for r in runs)
        for run in runs:
            if run["conclusion"] in FAILED_CONCLUSIONS:
                reasons.extend(failure_details(gh, repo, run))

        if not deployments:
            conclusions = {r["conclusion"] for r in runs}
            if conclusions & FAILED_CONCLUSIONS:
                status = "failure"
            elif None in conclusions:
                status = "in_progress"
            elif conclusions <= {"success", "skipped", "neutral"}:
                status = "success"
            else:
                status = ",".join(sorted(c for c in conclusions if c))
            info.update(
                deployment_status=status,
                deployment_started_at=min(r.get("run_started_at") or r["created_at"] for r in runs),
                deployment_finished_at="" if None in conclusions else max(r["updated_at"] for r in runs),
                deployed_by=", ".join(sorted({r["actor"]["login"] for r in runs if r.get("actor")})),
            )

    info["failure_reason"] = " | ".join(reasons)
    return info


def build_row(gh, repo_cfg, pr):
    repo = repo_cfg["name"]
    approvers, changes_requested = review_summary(gh, repo, pr["number"])
    created_at, merged_at = parse_ts(pr["created_at"]), parse_ts(pr["merged_at"])
    row = {
        "repo": repo,
        "pr_number": pr["number"],
        "pr_title": pr["title"],
        "pr_url": pr["html_url"],
        "author": pr["user"]["login"],
        "created_at": pr["created_at"],
        "merged_at": pr["merged_at"],
        "hours_to_merge": round((merged_at - created_at).total_seconds() / 3600, 2),
        "merged_by": (pr.get("merged_by") or {}).get("login", ""),
        "approvers": ", ".join(approvers),
        "changes_requested_by": ", ".join(changes_requested),
        "head_branch": pr["head"]["ref"],
        "labels": ", ".join(label["name"] for label in pr.get("labels", [])),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "changed_files": pr.get("changed_files"),
        "merge_commit_sha": pr["merge_commit_sha"],
    }
    row.update(deployment_info(gh, repo, pr["merge_commit_sha"], repo_cfg["environment"], repo_cfg["workflows"]))
    return row


def write_daily_csv(output_dir, day, rows, processed_repos):
    path = output_dir / f"{day.isoformat()}.csv"
    existing = []
    if path.exists():
        # Keep rows for repos not processed in this run so partial runs don't wipe them.
        with open(path, newline="", encoding="utf-8") as f:
            existing = [r for r in csv.DictReader(f) if r["repo"] not in processed_repos]
    all_rows = sorted(existing + rows, key=lambda r: (r["merged_at"], r["repo"]))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(all_rows)
    return path, len(all_rows)


def parse_args():
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=BASE_DIR / "repos.yaml")
    parser.add_argument("--output-dir", type=Path, default=BASE_DIR / "output")
    parser.add_argument("--date", type=date.fromisoformat, default=yesterday, help="UTC day to report (default: yesterday)")
    parser.add_argument("--days", type=int, default=1, help="number of days ending at --date to backfill")
    parser.add_argument("--repo", action="append", help="name or owner/name; overrides the config repo list (repeatable)")
    return parser.parse_args()


def main():
    args = parse_args()
    load_dotenv(BASE_DIR / ".env")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is not set (expected in .env or the environment)")

    repos, defaults = load_config(args.config)
    if args.repo:
        by_name = {r["name"]: r for r in repos}
        cli_repos = [make_repo(name, defaults) for name in args.repo]
        repos = [by_name.get(r["name"], r) for r in cli_repos]
    if not repos:
        sys.exit(f"No repos configured in {args.config}")

    first_day = args.date - timedelta(days=args.days - 1)
    since = datetime.combine(first_day, time.min, timezone.utc)
    until = datetime.combine(args.date + timedelta(days=1), time.min, timezone.utc)

    gh = GitHub(token)
    rows_by_day = {first_day + timedelta(days=i): [] for i in range(args.days)}
    processed, failed = set(), []

    for repo_cfg in repos:
        repo = repo_cfg["name"]
        print(f"{repo}: scanning PRs merged to {repo_cfg['base_branch']} {first_day}..{args.date}", file=sys.stderr)
        try:
            prs = merged_prs(gh, repo, repo_cfg["base_branch"], since, until)
            repo_rows = [build_row(gh, repo_cfg, pr) for pr in prs]
        except requests.HTTPError as e:
            print(f"{repo}: failed: {e}", file=sys.stderr)
            failed.append(repo)
            continue
        processed.add(repo)
        for row in repo_rows:
            rows_by_day[parse_ts(row["merged_at"]).date()].append(row)
        print(f"{repo}: {len(repo_rows)} merged PR(s)", file=sys.stderr)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for day, rows in rows_by_day.items():
        path, total = write_daily_csv(args.output_dir, day, rows, processed)
        print(f"wrote {path} ({total} rows)", file=sys.stderr)

    if failed:
        sys.exit(f"Failed repos: {', '.join(failed)}")


if __name__ == "__main__":
    main()
