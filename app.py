# app.py
from flask import Flask, request, jsonify
import requests
import os
from datetime import datetime, timedelta, timezone
from collections import Counter
import threading
import signal
import sys
from dotenv import load_dotenv
from typing import List, Dict, Optional, Any

# import io
# import matplotlib.pyplot as plt
# import pandas as pd
# from flask import send_file


# -------------------------
# Load environment
# -------------------------
load_dotenv()

# -------------------------
# Config
# -------------------------
POLL_INTERVAL: int = 30  # seconds
EVENT_RETENTION_MINUTES: int = 120  # keep events for 2 hours
EVENTS_OF_INTEREST: List[str] = ["PullRequestEvent", "WatchEvent", "IssuesEvent"]

# -------------------------
# In-memory storage
# -------------------------
events: List[Dict[str, Any]] = []

# Stop flag for graceful shutdown
stop_event = threading.Event()

# -------------------------
# GitHub polling function
# -------------------------
def fetch_github_events() -> None:
    """
    Polls the GitHub Events API for specific event types and stores them in memory.
    Old events beyond EVENT_RETENTION_MINUTES are pruned.
    This function reschedules itself using threading.Timer unless stop_event is set.
    """
    global events

    if stop_event.is_set():
        return  # stop polling gracefully

    try:
        url: str = "https://api.github.com/events"
        headers: Dict[str, str] = {"Accept": "application/vnd.github.v3+json"}
        token: Optional[str] = os.getenv("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"token {token}"

        response = requests.get(url, headers=headers)
        data = response.json()

        # Handle API errors (rate limit, etc.)
        if isinstance(data, dict) and "message" in data:
            print("GitHub API error:", data["message"])
            return

        for event in data:
            if event["type"] in EVENTS_OF_INTEREST:
                repo_name: str = event["repo"]["name"]
                events.append({
                    "type": event["type"],
                    "repo": repo_name,
                    "created_at": datetime.strptime(event["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                })
                print("Collected event for repo:", repo_name)

        # Prune old events
        cutoff: datetime = datetime.now(timezone.utc) - timedelta(minutes=EVENT_RETENTION_MINUTES)
        events = [e for e in events if e["created_at"] >= cutoff]

    except Exception as e:
        print("Error fetching GitHub events:", e)
    finally:
        if not stop_event.is_set():
            threading.Timer(POLL_INTERVAL, fetch_github_events).start()

# -------------------------
# Metrics functions
# -------------------------
def average_pr_time(repo_name: str) -> Optional[float]:
    """
    Calculate the average time in seconds between PullRequestEvents for a given repository.

    Args:
        repo_name: str - full repo name (e.g., "mrodriguezg1991/vulnscout")

    Returns:
        float | None: Average time in seconds, or None if less than 2 PR events exist.
    """
    pr_events: List[Dict[str, Any]] = [
        e for e in events if e["type"] == "PullRequestEvent" and e["repo"] == repo_name
    ]
    pr_events.sort(key=lambda x: x["created_at"])
    if len(pr_events) < 2:
        return None
    time_diffs: List[float] = [
        (pr_events[i]["created_at"] - pr_events[i-1]["created_at"]).total_seconds()
        for i in range(1, len(pr_events))
    ]
    return sum(time_diffs) / len(time_diffs)


def count_events(offset_minutes: int) -> Counter:
    """
    Count events of interest that occurred in the last `offset_minutes`.

    Args:
        offset_minutes: int - time window in minutes

    Returns:
        Counter: counts per event type
    """
    now: datetime = datetime.now(timezone.utc)
    cutoff: datetime = now - timedelta(minutes=offset_minutes)
    recent_events: List[Dict[str, Any]] = [e for e in events if e["created_at"] >= cutoff]
    return Counter(e["type"] for e in recent_events)

# -------------------------
# Flask app
# -------------------------
app = Flask(__name__)

@app.route("/metrics/repos")
def list_repos() -> Dict[str, List[str]]:
    """Return a list of unique repository names currently in memory."""
    unique_repos: List[str] = list({e["repo"] for e in events})
    return {"repos": unique_repos}

@app.route("/metrics/pr_average")
def pr_average() -> Any:
    """
    Return average time between PullRequestEvents for a given repo.
    Query parameter: repo=<owner/repo>
    """
    repo: Optional[str] = request.args.get("repo")
    if not repo:
        return jsonify({"error": "Please provide repo parameter"}), 400
    avg: Optional[float] = average_pr_time(repo)
    if avg is None:
        return jsonify({
            "repo": repo,
            "average_time_seconds": None,
            "message": "Not enough PR events yet"
        })
    return jsonify({"repo": repo, "average_time_seconds": avg})

@app.route("/metrics/events_count")
def events_count() -> Any:
    """Return count of events grouped by type for a given offset in minutes (default 10)."""
    offset: int = int(request.args.get("offset", 10))
    counts: Counter = count_events(offset)
    return jsonify(counts)

@app.route("/metrics/top_repos")
def top_repos() -> Any:
    """
    Return top N repositories by number of PullRequestEvents in memory.
    Query parameter: n=<number> (default: 5)
    Only PullRequestEvent events are considered.
    """
    top_n: int = int(request.args.get("n", 5))
    # Filter only PullRequestEvent
    pr_events: List[Dict[str, Any]] = [e for e in events if e["type"] == "PullRequestEvent"]
    repo_counts: Counter = Counter(e["repo"] for e in pr_events)
    top_list: List[Dict[str, Any]] = [
        {"repo": repo, "event_count": count} 
        for repo, count in repo_counts.most_common(top_n)
    ]
    return jsonify(top_list)

@app.route("/debug/events")
def debug_events() -> Dict[str, List[Dict[str, Any]]]:
    """Return the first 5 events in memory for debugging purposes."""
    return {"sample_events": events[:5]}

# This does not seem to be working properly
# @app.route("/metrics/plot_pr_times")
# def plot_pr_times() -> Any:
#     """
#     Generate a visualization of PullRequestEvent intervals for a given repository.
#     Query parameter: repo=<owner/repo>
#     Returns: PNG image
#     """
#     repo_name = request.args.get("repo")
#     if not repo_name:
#         return jsonify({"error": "Please provide repo parameter"}), 400

#     # Filter PullRequestEvents for this repo
#     pr_events = [e for e in events if e["type"] == "PullRequestEvent" and e["repo"] == repo_name]
#     if len(pr_events) < 2:
#         return jsonify({"message": "Not enough PR events to plot"}), 400

#     # Sort by creation time
#     pr_events.sort(key=lambda x: x["created_at"])
#     times = [e["created_at"] for e in pr_events]

#     # Calculate intervals in seconds
#     intervals = [(times[i] - times[i-1]).total_seconds() for i in range(1, len(times))]

#     # Create DataFrame for plotting
#     df = pd.DataFrame({"interval_seconds": intervals, "index": range(1, len(times))})

#     # Plot
#     plt.figure(figsize=(8, 4))
#     plt.plot(df["index"], df["interval_seconds"], marker='o')
#     plt.title(f"Pull Request Intervals for {repo_name}")
#     plt.xlabel("PR Sequence")
#     plt.ylabel("Interval (seconds)")
#     plt.grid(True)

#     # Save to BytesIO and send as response
#     buf = io.BytesIO()
#     plt.savefig(buf, format="png")
#     buf.seek(0)
#     plt.close()
#     return send_file(buf, mimetype='image/png')

# -------------------------
#  Shutdown
# -------------------------
def handle_shutdown(sig: int, frame: Any) -> None:
    """Stop background polling and exit Flask app gracefully."""
    print("\nShutting down gracefully...")
    stop_event.set()
    sys.exit(0)

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

# -------------------------
# Start background polling
# -------------------------
fetch_github_events()

# -------------------------
# Run Flask
# -------------------------
if __name__ == "__main__":
    app.run(debug=True)
