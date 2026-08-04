#!/usr/bin/env python3
"""Re-host the most-recently-read Slack image as an optimized feed thumbnail.

WHY THIS EXISTS: the Slack MCP connector *renders* an image to the agent rather
than returning the raw file bytes, so `slack_read_file`'s result cannot be saved
to disk directly. The rendered copy (good resolution, ~1180px in practice) is
written into the live session transcript jsonl; this script recovers the newest
image blob from there, then downsizes/compresses it to a feed thumbnail.

USAGE:
    python3 tools/rehost_slack_image.py <output_path.jpg>

Run it IMMEDIATELY after a SINGLE slack_read_file call, and before reading or
viewing any other image, so that "newest image blob" maps to the file you just
read. Do one (read -> rehost) pair at a time.

NOTE: this is transcript recovery, which is inherently more brittle than a clean
download. The robust long-term upgrade is a Slack bot/user token so the run can
fetch url_private_download directly.
"""
import sys, os, glob, json, base64, io, subprocess
try:
    from PIL import Image
except ImportError:
    # fresh scheduled sessions may not have Pillow; self-bootstrap it
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "--break-system-packages", "pillow"], check=True)
    from PIL import Image

TARGET_W = 800      # feed cards render ~168px; 800 keeps it crisp on retina
QUALITY  = 82

def newest_transcript():
    paths = glob.glob("/root/.claude/projects/**/*.jsonl", recursive=True)
    if not paths:
        sys.exit("ERROR: no session transcript jsonl found")
    return max(paths, key=os.path.getmtime)

def collect_blobs(obj, out):
    if isinstance(obj, dict):
        src = obj.get("source")
        if isinstance(src, dict) and src.get("type") == "base64" and "data" in src:
            out.append(src["data"])
        for v in obj.values():
            collect_blobs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_blobs(v, out)

def main():
    if len(sys.argv) < 2:
        sys.exit("usage: rehost_slack_image.py <output_path.jpg>")
    out_path = sys.argv[1]
    tp = newest_transcript()
    blobs = []
    with open(tp) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            collect_blobs(rec, blobs)
    if not blobs:
        sys.exit("ERROR: no image blobs found in transcript " + tp)
    raw = base64.b64decode(blobs[-1])
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = im.size
    if w > TARGET_W:
        im = im.resize((TARGET_W, round(h * TARGET_W / w)), Image.LANCZOS)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    im.save(out_path, "JPEG", quality=QUALITY, optimize=True)
    print("saved %s | from %s | orig %dx%d -> %dx%d | %d bytes"
          % (out_path, os.path.basename(tp), w, h, im.size[0], im.size[1],
             os.path.getsize(out_path)))

if __name__ == "__main__":
    main()
