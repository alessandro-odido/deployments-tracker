"""Streamlit dashboard over the daily CSVs in output/. Run: streamlit run dashboard.py"""

from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
FAILED = {"failure", "error"}
PENDING = {"waiting", "queued", "pending", "in_progress"}

st.set_page_config(page_title="Deployment History", layout="wide")


@st.cache_data(ttl=300)
def load_data():
    frames = [pd.read_csv(f) for f in sorted(OUTPUT_DIR.glob("*.csv"))]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)

    for col in ["created_at", "merged_at", "deployment_started_at", "deployment_finished_at"]:
        df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)

    df["repo_short"] = df["repo"].str.split("/").str[-1]
    df["date"] = df["merged_at"].dt.date
    df["deployment_duration_min"] = (df["deployment_finished_at"] - df["deployment_started_at"]).dt.total_seconds() / 60
    df["merge_to_deploy_min"] = (df["deployment_finished_at"] - df["merged_at"]).dt.total_seconds() / 60
    df["outcome"] = df["deployment_status"].map(
        lambda s: "success" if s == "success" else "failed" if s in FAILED else "pending" if s in PENDING else s
    )
    return df


df = load_data()
st.title("Deployment History")

if df.empty:
    st.info(f"No data in {OUTPUT_DIR}. Run track_deployments.py first.")
    st.stop()

with st.sidebar:
    st.header("Filters")
    min_day, max_day = df["date"].min(), df["date"].max()
    day_range = st.date_input("Merged between", (min_day, max_day), min_value=min_day, max_value=max_day)
    repos = st.multiselect("Repo (empty = all)", sorted(df["repo_short"].unique()))
    statuses = st.multiselect("Deployment status (empty = all)", sorted(df["deployment_status"].dropna().unique()))
    authors = st.multiselect("Author (empty = all)", sorted(df["author"].dropna().unique()))
    if st.button("Reload data"):
        load_data.clear()
        st.rerun()

mask = pd.Series(True, index=df.index)
# date_input returns a single date while the user is still picking the range end.
if isinstance(day_range, (list, tuple)) and len(day_range) == 2:
    mask &= df["date"].between(*day_range)
if repos:
    mask &= df["repo_short"].isin(repos)
if statuses:
    mask &= df["deployment_status"].isin(statuses)
if authors:
    mask &= df["author"].isin(authors)
filtered = df[mask]

if filtered.empty:
    st.warning("No rows match the current filters.")
    st.stop()

st.subheader("Summary")
deployed = filtered[filtered["outcome"].isin(["success", "failed"])]
success_rate = (deployed["outcome"] == "success").mean() * 100 if len(deployed) else None
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Merged PRs", len(filtered))
c2.metric("Successful", int((filtered["outcome"] == "success").sum()))
c3.metric("Failed", int((filtered["outcome"] == "failed").sum()))
c4.metric("Pending", int((filtered["outcome"] == "pending").sum()))
c5.metric("Success rate", f"{success_rate:.0f}%" if success_rate is not None else "n/a")
c6.metric("Median merge → deploy", f"{filtered['merge_to_deploy_min'].median():.0f} min" if filtered["merge_to_deploy_min"].notna().any() else "n/a")

st.subheader("Merges per day")
daily = filtered.groupby(["date", "deployment_status"]).size().reset_index(name="count")
st.bar_chart(daily, x="date", y="count", color="deployment_status")

st.subheader("Merges per repo")
per_repo = filtered.groupby(["repo_short", "deployment_status"]).size().reset_index(name="count")
# labelLimit=0 keeps the full repo name from being truncated on the axis.
repo_chart = (
    alt.Chart(per_repo)
    .mark_bar()
    .encode(
        x=alt.X("count:Q", title="count"),
        y=alt.Y("repo_short:N", title="repo", sort="-x", axis=alt.Axis(labelLimit=0)),
        color="deployment_status:N",
    )
    .properties(height=max(300, 28 * per_repo["repo_short"].nunique()))
)
st.altair_chart(repo_chart, use_container_width=True)

left, right = st.columns(2)
with left:
    st.subheader("Top authors")
    st.bar_chart(filtered["author"].value_counts().head(15), horizontal=True)
with right:
    st.subheader("Hours to merge (per PR)")
    st.scatter_chart(filtered, x="merged_at", y="hours_to_merge", color="repo_short")

failures = filtered[filtered["outcome"] == "failed"]
if not failures.empty:
    st.subheader("Failed deployments")
    st.dataframe(
        failures[["merged_at", "repo_short", "pr_number", "pr_title", "author", "failure_reason", "pr_url"]]
        .sort_values("merged_at", ascending=False),
        width="stretch",
        hide_index=True,
        column_config={"pr_url": st.column_config.LinkColumn("PR", display_text="open")},
    )

st.subheader("Deployment records")
show_cols = [
    "merged_at",
    "repo_short",
    "pr_number",
    "pr_title",
    "author",
    "approvers",
    "merged_by",
    "hours_to_merge",
    "deployment_status",
    "deployment_environment",
    "deployed_by",
    "deployment_duration_min",
    "merge_to_deploy_min",
    "pr_url",
]
st.dataframe(
    filtered[show_cols].sort_values("merged_at", ascending=False),
    width="stretch",
    hide_index=True,
    column_config={
        "pr_url": st.column_config.LinkColumn("PR", display_text="open"),
        "deployment_duration_min": st.column_config.NumberColumn("deploy duration (min)", format="%.1f"),
        "merge_to_deploy_min": st.column_config.NumberColumn("merge → deploy (min)", format="%.1f"),
    },
)
st.download_button("Download filtered CSV", filtered.to_csv(index=False), "deployments_filtered.csv", "text/csv")
