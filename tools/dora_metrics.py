#!/usr/bin/env python3
"""DORA-ның төрт метрикасын GitHub REST API арқылы есептеу және дашборд салу.

Деректер көздері (тек репозиторийдің нақты деректері):
  * Deployment      -> GitHub Releases (жарияланған, draft емес)
  * Lead time       -> біріктірілген PR: бірінші коммит -> келесі релиз
  * Change failure  -> релизден кейін 48 сағат ішінде біріктірілген hotfix PR
                       (label `hotfix` немесе бұтақ аты `hotfix/` деп басталады)
  * MTTR            -> label `incident` бар жабылған issue: closed_at - created_at

Қолдану:
  GITHUB_TOKEN=... python tools/dora_metrics.py --repo OWNER/REPO --days 90 --out dora-out
"""
import argparse
import json
import os
import statistics
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.github.com"
HOTFIX_WINDOW = timedelta(hours=48)


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


# ---------------------------------------------------------------- GitHub API
def gh_get(path, token, params=None):
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url, out = f"{API}{path}", []
    params = dict(params or {}, per_page=100)
    while url:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        out.extend(data if isinstance(data, list) else [data])
        url = r.links.get("next", {}).get("url")
        params = None
    return out


def fetch(repo, token, since):
    releases = [
        {"tag": r["tag_name"], "at": ts(r["published_at"])}
        for r in gh_get(f"/repos/{repo}/releases", token)
        if not r["draft"] and r["published_at"]
    ]
    prs = []
    for p in gh_get(f"/repos/{repo}/pulls", token, {"state": "closed", "base": "main"}):
        if not p["merged_at"] or ts(p["merged_at"]) < since:
            continue
        commits = gh_get(f"/repos/{repo}/pulls/{p['number']}/commits", token)
        first = min(ts(c["commit"]["author"]["date"]) for c in commits) if commits else ts(p["created_at"])
        labels = {l["name"].lower() for l in p["labels"]}
        prs.append({
            "number": p["number"],
            "first_commit": first,
            "merged": ts(p["merged_at"]),
            "hotfix": "hotfix" in labels or p["head"]["ref"].startswith("hotfix/"),
        })
    incidents = [
        {"number": i["number"], "created": ts(i["created_at"]), "closed": ts(i["closed_at"])}
        for i in gh_get(f"/repos/{repo}/issues", token, {"state": "closed", "labels": "incident"})
        if "pull_request" not in i and i["closed_at"] and ts(i["closed_at"]) >= since
    ]
    return releases, prs, incidents


# ------------------------------------------------------------------- метрика
def compute(releases, prs, incidents, since, now):
    rel = sorted((r for r in releases if r["at"] >= since), key=lambda r: r["at"])
    days = max((now - since).days, 1)

    deploy_per_week = len(rel) / (days / 7)

    leads = []
    for p in prs:
        nxt = next((r for r in sorted(releases, key=lambda r: r["at"]) if r["at"] >= p["merged"]), None)
        if nxt:
            leads.append((nxt["at"] - p["first_commit"]).total_seconds() / 3600)

    hotfix_merges = [p["merged"] for p in prs if p["hotfix"]]
    failed = [r for r in rel if any(r["at"] <= m <= r["at"] + HOTFIX_WINDOW for m in hotfix_merges)]
    cfr = len(failed) / len(rel) * 100 if rel else None

    mttr_list = [(i["closed"] - i["created"]).total_seconds() / 3600 for i in incidents]

    return {
        "window_days": days,
        "deployments": len(rel),
        "deploy_per_week": round(deploy_per_week, 2),
        "lead_time_h_median": round(statistics.median(leads), 1) if leads else None,
        "lead_time_samples": len(leads),
        "change_failure_rate_pct": round(cfr, 1) if cfr is not None else None,
        "failed_deployments": len(failed),
        "mttr_h_mean": round(statistics.mean(mttr_list), 1) if mttr_list else None,
        "incidents": len(mttr_list),
        "_leads": leads,
        "_mttr": mttr_list,
        "_rel": rel,
        "_failed": failed,
    }


def level(m):
    """DORA сынып шектері (dora.dev-тің жалпыланған бенчмарктері)."""
    def pick(v, bounds, reverse=False):
        if v is None:
            return "дерек жоқ"
        names = ["Elite", "High", "Medium", "Low"]
        for n, b in zip(names, bounds):
            if (v >= b) if reverse else (v <= b):
                return n
        return "Low"
    return {
        "deploy_frequency": pick(m["deploy_per_week"], [7, 1, 0.25], reverse=True),
        "lead_time": pick(m["lead_time_h_median"], [24, 168, 720]),
        "change_failure_rate": pick(m["change_failure_rate_pct"], [5, 10, 15]),
        "mttr": pick(m["mttr_h_mean"], [1, 24, 168]),
    }


# ------------------------------------------------------------------- дашборд
def plot(m, lv, repo, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    c, bad, grey = "#2b6cb0", "#c53030", "#718096"
    fig, ax = plt.subplots(2, 2, figsize=(12, 7.5))
    fig.suptitle(f"DORA dashboard — {repo} (соңғы {m['window_days']} күн)", fontsize=14, fontweight="bold")

    # 1 Deployment frequency (апталар бойынша)
    a = ax[0][0]
    weeks = {}
    for r in m["_rel"]:
        k = r["at"].strftime("%G-W%V")
        weeks[k] = weeks.get(k, 0) + 1
    a.bar(list(weeks) or ["—"], list(weeks.values()) or [0], color=c)
    a.set_title(f"Deployment frequency: {m['deploy_per_week']}/апта  [{lv['deploy_frequency']}]", fontsize=10)
    a.tick_params(axis="x", rotation=60, labelsize=7)
    a.set_ylabel("релиз саны")

    # 2 Lead time
    a = ax[0][1]
    a.plot(range(1, len(m["_leads"]) + 1), m["_leads"], marker="o", color=c)
    if m["lead_time_h_median"] is not None:
        a.axhline(m["lead_time_h_median"], color=grey, ls="--", lw=1)
    a.set_title(f"Lead time (медиана): {m['lead_time_h_median']} сағ  [{lv['lead_time']}]", fontsize=10)
    a.set_xlabel("PR (реттік нөмір)")
    a.set_ylabel("сағат")

    # 3 Change failure rate
    a = ax[1][0]
    ok = m["deployments"] - m["failed_deployments"]
    a.bar(["сәтті", "hotfix қажет болды"], [ok, m["failed_deployments"]], color=[c, bad])
    a.set_title(f"Change failure rate: {m['change_failure_rate_pct']}%  [{lv['change_failure_rate']}]", fontsize=10)
    a.set_ylabel("релиз саны")

    # 4 MTTR
    a = ax[1][1]
    a.bar([f"#{i+1}" for i in range(len(m["_mttr"]))] or ["—"], m["_mttr"] or [0], color=c)
    a.set_title(f"MTTR (орташа): {m['mttr_h_mean']} сағ  [{lv['mttr']}]", fontsize=10)
    a.set_ylabel("сағат")

    for x in ax.flat:
        x.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(path, dpi=150)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default="dora-out")
    a = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=a.days)

    releases, prs, incidents = fetch(a.repo, token, since)
    m = compute(releases, prs, incidents, since, now)
    lv = level(m)

    os.makedirs(a.out, exist_ok=True)
    public = {k: v for k, v in m.items() if not k.startswith("_")}
    with open(os.path.join(a.out, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump({"repo": a.repo, "generated": now.isoformat(), **public, "levels": lv}, f,
                  ensure_ascii=False, indent=2)
    plot(m, lv, a.repo, os.path.join(a.out, "dora_dashboard.png"))
    print(json.dumps({**public, "levels": lv}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
