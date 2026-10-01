# deployments-tracker

Lists PRs merged to `main` in a set of GitHub repos, along with the deployment each merge triggered. Results go to one CSV per day in `output/YYYY-MM-DD.csv`.

Each row covers one merged PR:
- the author, the approvers, and who merged it
- when it was opened and merged, and how long that took
- the diff size
- the deployment status, environment, and timing
- the workflow runs, and the reason for any failure

## Setup

### 1. Create a GitHub classic PAT

1. On GitHub, open your profile picture, then **Settings**, then **Developer settings**.
2. Go to **Personal access tokens**, then **Tokens (classic)**, then **Generate new token (classic)**.
3. Give it a name and an expiration date, and select only the **`repo`** scope.
4. Generate the token and copy it.
5. In the token list, click **Configure SSO** next to the new token and **Authorize** it for the organization.

### 2. Install dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Add the token to `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Then edit `.env`:

```
GITHUB_TOKEN=ghp_...
```

`.env` is in `.gitignore`. Never commit it.

### 4. Configure `repos.yaml`

```yaml
defaults:
  owner: Odido-Datascience-BI-Analytics   # used for entries without an owner/ prefix
  base_branch: main
  environment: deploy-higher-environments # GitHub deployment environment; null = any
  workflows: []                           # deploy workflow file names; empty = all push workflows

repos:
  - repo-name
  - other-owner/repo-name
  - name: another-repo                    # per-repo overrides
    environment: production
    workflows: [deploy.yml]
```

To skip a repo, comment out its line.

## Usage

```bash
.venv/bin/python track_deployments.py                                  # yesterday (UTC), all configured repos
.venv/bin/python track_deployments.py --date 2026-09-30                # a specific day
.venv/bin/python track_deployments.py --date 2026-09-30 --days 7       # backfill 7 days, one CSV per day
.venv/bin/python track_deployments.py --repo repo-name                 # a single repo, ignoring the config list
```

Re-running for a day replaces that day's rows for the repos processed and keeps rows for all other repos.

Deployments that are still running when the script runs show as `in_progress`. To avoid this, run it for the previous day.

## Dashboard

```bash
.venv/bin/streamlit run dashboard.py
```

The dashboard opens in your browser and reads every CSV in `output/`.
