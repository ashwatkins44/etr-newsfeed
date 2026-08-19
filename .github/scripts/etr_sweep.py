#!/usr/bin/env python3
"""
ETR News Feed sweep — runs in GitHub Actions (no Claude / Cowork in the loop).

Reads the #etr-newsfeed Slack channel via the Slack Web API (bot token in
SLACK_TOKEN), finds messages that have a URL AND a :white_check_mark: reaction,
and writes new items to candidates.json. update_feed.py then merges them into
feed.json (dedupe by sourceId). Screenshots dropped in a post's thread are
downloaded with the token and re-hosted under images/; article images come from
og:image; social posts without a screenshot get no image (widget shows a gradient).

Env:
  SLACK_TOKEN   Slack bot token (xoxb-...) that is a MEMBER of the channel.
  CHANNEL_ID    optional; defaults to C0BLJKA4YQP.
"""
import os, re, sys, json, io, time, html
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, urlsplit, urlunsplit

import requests
from PIL import Image
from bs4 import BeautifulSoup

CHANNEL_ID = os.environ.get("CHANNEL_ID", "C0BLJKA4YQP")
TOKEN = os.environ.get("SLACK_TOKEN", "")
RAW_BASE = "https://raw.githubusercontent.com/ashwatkins44/etr-newsfeed/main/images/"
SOCIAL_DOMAINS = ("instagram.com", "x.com", "twitter.com", "tiktok.com", "threads.net", "facebook.com")
PLATFORM = {"instagram.com": "Instagram", "x.com": "X", "twitter.com": "X",
            "tiktok.com": "TikTok", "threads.net": "Threads", "facebook.com": "Facebook"}
PT = ZoneInfo("America/Los_Angeles")
LINK_RE = re.compile(r"<(https?://[^|>]+)(?:\|[^>]*)?>")
UA = "Mozilla/5.0 (compatible; ETRNewsBot/1.0; +https://eattalkrepeat.com)"

if not TOKEN:
    sys.exit("ERROR: SLACK_TOKEN is not set.")


def slack(method, **params):
    """Call a Slack Web API method (GET)."""
    r = requests.get("https://slack.com/api/" + method,
                     headers={"Authorization": f"Bearer {TOKEN}"},
                     params=params, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {data.get('error')}")
    return data


def existing_source_ids():
    ids = set()
    for fn in ("feed.json", "archive.json"):
        if os.path.exists(fn):
            try:
                d = json.load(open(fn))
                items = d["items"] if isinstance(d, dict) else d
                ids.update(it.get("sourceId") for it in items)
            except Exception as e:
                print(f"warn: could not read {fn}: {e}")
    return ids


def first_url_and_text(text):
    """Return (first_http_url, human_text_without_link_tokens)."""
    text = text or ""
    m = LINK_RE.search(text)
    url = m.group(1) if m else None
    cleaned = LINK_RE.sub(lambda mm: "", text)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = html.unescape(cleaned).strip()
    return url, cleaned


def is_social(url):
    host = (urlparse(url).hostname or "").lower()
    for d in SOCIAL_DOMAINS:
        if host == d or host.endswith("." + d):
            return d
    return None


def strip_query(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def slugify(text, fallback):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = "-".join(s.split("-")[:6])
    return s or fallback


def category_from_replies(replies):
    txt = " ".join((r.get("text") or "") for r in replies).lower()
    if re.search(r"\bopen(ing|ings|s|ed)?\b", txt): return "Openings"
    if re.search(r"\bclos(e|ed|ing|ings)\b", txt):  return "Closings"
    if "beyond" in txt: return "Beyond Vegas"
    if re.search(r"\bevents?\b", txt): return "Events"
    return "News"


def find_screenshot(main_msg, replies):
    for msg in [main_msg] + list(replies):
        for f in (msg.get("files") or []):
            if str(f.get("mimetype", "")).startswith("image/") and f.get("url_private_download"):
                return f
    return None


def rehost_screenshot(f, slug):
    """Download the Slack file with the token, optimize to an 800px JPEG under images/."""
    r = requests.get(f["url_private_download"],
                     headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60)
    r.raise_for_status()
    im = Image.open(io.BytesIO(r.content)).convert("RGB")
    w, h = im.size
    if w > 800:
        im = im.resize((800, round(h * 800 / w)), Image.LANCZOS)
    os.makedirs("images", exist_ok=True)
    name = f"{slug}.jpg"
    if os.path.exists(os.path.join("images", name)):
        name = f"{slug}-{f.get('id','x')[-6:]}.jpg"
    im.save(os.path.join("images", name), "JPEG", quality=82, optimize=True)
    print(f"  re-hosted screenshot -> images/{name} ({im.size[0]}x{im.size[1]})")
    return RAW_BASE + name


def fetch_article(url):
    """Return (title, description, image, source) from og/twitter meta."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        def meta(*names):
            for n in names:
                el = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
                if el and el.get("content"):
                    return el["content"].strip()
            return ""

        title = meta("og:title", "twitter:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
        desc = meta("og:description", "twitter:description")
        image = meta("og:image", "twitter:image", "twitter:image:src")
        source = meta("og:site_name") or (urlparse(url).hostname or "").replace("www.", "")
        if image and image.startswith("//"):
            image = "https:" + image
        if image and not image.startswith("http"):
            image = ""
        if desc and len(desc) > 200:
            desc = desc[:197].rstrip() + "..."
        return title, desc, image, source
    except Exception as e:
        print(f"  article fetch failed ({url}): {e}")
        return "", "", "", (urlparse(url).hostname or "").replace("www.", "")


def build_item(msg, replies):
    ts = msg["ts"]
    url, human = first_url_and_text(msg.get("text"))
    if not url:
        return None
    date = datetime.fromtimestamp(float(ts), tz=PT).strftime("%Y-%m-%d")
    source_id = f"{CHANNEL_ID}-{ts}"
    category = category_from_replies(replies)
    shot = find_screenshot(msg, replies)
    social = is_social(url)

    title = human
    image = ""
    source = ""
    blurb = ""

    if social:
        url = strip_query(url)
        source = PLATFORM.get(social, social.split(".")[0].title())
        if not title:
            title = url.rstrip("/").split("/")[-1] or source
    else:
        a_title, a_desc, a_image, a_source = fetch_article(url)
        title = human or a_title or (urlparse(url).hostname or url)
        blurb = a_desc
        source = a_source or "eattalkrepeat.com"
        image = a_image

    if shot:
        try:
            image = rehost_screenshot(shot, slugify(title, ts.replace(".", "")))
        except Exception as e:
            print(f"  screenshot re-host failed: {e}")

    return {"title": title, "url": url, "image": image, "source": source,
            "category": category, "blurb": blurb, "date": date, "sourceId": source_id}


def main():
    existing = existing_source_ids()
    print(f"{len(existing)} sourceIds already published.")
    hist = slack("conversations.history", channel=CHANNEL_ID, limit=100)
    msgs = hist.get("messages", [])
    print(f"read {len(msgs)} messages.")

    new_items = []
    for msg in msgs:
        ts = msg.get("ts")
        if not ts:
            continue
        reactions = {r.get("name") for r in (msg.get("reactions") or [])}
        if "white_check_mark" not in reactions:
            continue
        url, _ = first_url_and_text(msg.get("text"))
        if not url:
            continue
        source_id = f"{CHANNEL_ID}-{ts}"
        if source_id in existing:
            continue
        replies = []
        if msg.get("reply_count"):
            try:
                rep = slack("conversations.replies", channel=CHANNEL_ID, ts=ts)
                replies = [m for m in rep.get("messages", []) if m.get("ts") != ts]
            except Exception as e:
                print(f"  replies fetch failed for {ts}: {e}")
            time.sleep(1)
        try:
            item = build_item(msg, replies)
        except Exception as e:
            print(f"  build failed for {ts}: {e}")
            item = None
        if item:
            print(f"  + [{item['category']}] {item['title'][:60]} "
                  f"(img: {'yes' if item['image'] else 'none'})")
            new_items.append(item)

    new_items.reverse()
    json.dump(new_items, open("candidates.json", "w"), ensure_ascii=False, indent=2)
    print(f"wrote candidates.json with {len(new_items)} new item(s).")


if __name__ == "__main__":
    main()
