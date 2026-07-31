#!/usr/bin/env python3
"""Merge approved candidate items into feed.json for the ETR news aggregator.
Reads candidates.json (list of new items gathered from Slack), dedupes against
feed.json + archive.json by sourceId, prepends new items, keeps the live feed
capped at CAP (overflow rolls into archive.json). Newest first."""
import json, os
from datetime import datetime, timezone

FEED, ARCHIVE, CAND, CAP = "feed.json", "archive.json", "candidates.json", 300

def load(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except Exception: return default
    return default

feed = load(FEED, {"updated": None, "items": []})
if isinstance(feed, list): feed = {"updated": None, "items": feed}
archive = load(ARCHIVE, {"items": []})
cands = load(CAND, [])
if isinstance(cands, dict): cands = cands.get("items", [])

seen = {it.get("sourceId") for it in feed.get("items", []) if it.get("sourceId")}
seen |= {it.get("sourceId") for it in archive.get("items", []) if it.get("sourceId")}

added = []
for c in cands:
    sid = c.get("sourceId")
    if sid and sid in seen:
        continue
    if sid: seen.add(sid)
    added.append(c)

items = added + feed.get("items", [])
items.sort(key=lambda it: it.get("date") or "", reverse=True)

overflow = items[CAP:]
items = items[:CAP]
if overflow:
    archive.setdefault("items", [])
    archive["items"] = overflow + archive["items"]
    with open(ARCHIVE, "w") as f: json.dump(archive, f, indent=2, ensure_ascii=False)

feed["items"] = items
feed["updated"] = datetime.now(timezone.utc).isoformat()
with open(FEED, "w") as f: json.dump(feed, f, indent=2, ensure_ascii=False)

print(f"Added {len(added)} new item(s). Live feed: {len(items)}. Rolled to archive: {len(overflow)}.")
for a in added:
    print(f"  + [{a.get('category','News')}] {a.get('title','(no title)')}")
