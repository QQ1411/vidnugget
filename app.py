"""VidNugget — YouTube → Knowledge Nugget web app."""

import os
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from processor import (
    process_video, load_all_nuggets, UPLOADS_DIR, KNOWLEDGE_BASE,
    INBOX_DIR, OBSIDIAN_QUEUE, extract_video_id
)

# In-memory job tracker
jobs: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(watch_inbox())
    asyncio.create_task(watch_obsidian_queue())
    yield

app = FastAPI(title="VidNugget", lifespan=lifespan)


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
LINKS_FILE = INBOX_DIR / "links.txt"
DONE_PREFIX = "✅ "
FAIL_PREFIX = "❌ "


def read_links_file() -> list[str]:
    if not LINKS_FILE.exists():
        return []
    return LINKS_FILE.read_text(encoding="utf-8").splitlines()


def write_links_file(lines: list[str]) -> None:
    LINKS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mark_line(lines: list[str], url: str, prefix: str, suffix: str = "") -> list[str]:
    """Replace the first line containing url with a marked version."""
    for i, line in enumerate(lines):
        if url in line:
            lines[i] = f"{prefix}{url}{suffix}"
            return lines
    return lines


async def watch_inbox():
    """
    Watch iCloud inbox/ for new YouTube URLs in links.txt.

    The user keeps one persistent file — iCloud Drive → VidNugget → inbox → links.txt —
    and pastes URLs into it one per line. VidNugget checks every 5 seconds, processes
    any unmarked lines, and updates each line in-place:

        https://youtube.com/watch?v=abc        ← pending
        ✅ https://youtube.com/watch?v=abc     ← done
        ❌ https://youtube.com/watch?v=xyz     ← failed

    Any images (.jpg/.png) in inbox/ at the time a URL is processed are used
    as screenshots for that video, then deleted from inbox/.
    """
    while True:
        try:
            lines = read_links_file()
            for i, line in enumerate(lines):
                stripped = line.strip()
                # Skip blank lines, comments, already-processed lines, and the header block
                if (not stripped
                        or stripped.startswith("#")
                        or stripped.startswith(DONE_PREFIX)
                        or stripped.startswith(FAIL_PREFIX)):
                    continue
                if "youtube.com" not in stripped and "youtu.be" not in stripped:
                    continue

                url = stripped
                video_id = extract_video_id(url)
                if not video_id:
                    lines[i] = f"{FAIL_PREFIX}{url}  # could not parse video ID"
                    write_links_file(lines)
                    continue

                # Mark as in-progress immediately so a restart doesn't double-process
                lines[i] = f"⏳ {url}"
                write_links_file(lines)
                lines = read_links_file()  # re-read so mark_line has fresh state

                # Grab images sitting in inbox/ right now
                screenshots = [
                    p for p in INBOX_DIR.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_EXTS
                ]

                job_id = f"inbox_{video_id}"
                jobs[job_id] = {"status": "processing", "url": url, "source": "icloud_inbox"}
                try:
                    result = await process_video(url, screenshots)
                    jobs[job_id].update({"status": "done", **result})
                    title = result.get("title", "")
                    lines = read_links_file()  # re-read in case user edited while processing
                    lines = mark_line(lines, url, DONE_PREFIX, f"  # {title}")
                    write_links_file(lines)
                    for img in screenshots:
                        img.unlink(missing_ok=True)
                except Exception as e:
                    jobs[job_id].update({"status": "error", "error": str(e)})
                    lines = read_links_file()
                    lines = mark_line(lines, url, FAIL_PREFIX, f"  # {e}")
                    write_links_file(lines)

        except Exception:
            pass

        await asyncio.sleep(5)


async def watch_obsidian_queue():
    """
    Watch the Obsidian 'VidNugget Queue.md' note for new YouTube URLs.
    User shares from YouTube → Obsidian (iOS share sheet) → appends URL to this note.
    Works anywhere — syncs via iCloud, Mac processes when it sees the change.
    """
    while True:
        try:
            if not OBSIDIAN_QUEUE.exists():
                await asyncio.sleep(10)
                continue

            lines = OBSIDIAN_QUEUE.read_text(encoding="utf-8").splitlines()
            changed = False

            for i, line in enumerate(lines):
                stripped = line.strip()
                if (not stripped
                        or stripped.startswith("#")
                        or stripped.startswith("|")
                        or stripped.startswith("✅")
                        or stripped.startswith("⏳")
                        or stripped.startswith("❌")
                        or stripped == "---"):
                    continue
                if "youtube.com" not in stripped and "youtu.be" not in stripped:
                    continue

                url = stripped
                video_id = extract_video_id(url)
                if not video_id:
                    lines[i] = f"❌ {url}  <!-- could not parse video ID -->"
                    changed = True
                    continue

                # Mark in-progress right away
                lines[i] = f"⏳ {url}"
                OBSIDIAN_QUEUE.write_text("\n".join(lines) + "\n", encoding="utf-8")
                changed = False  # just wrote it

                screenshots = [
                    p for p in INBOX_DIR.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_EXTS
                ]

                job_id = f"obsidian_{video_id}"
                jobs[job_id] = {"status": "processing", "url": url, "source": "obsidian"}
                try:
                    result = await process_video(url, screenshots)
                    jobs[job_id].update({"status": "done", **result})
                    title = result.get("title", "")
                    lines = OBSIDIAN_QUEUE.read_text(encoding="utf-8").splitlines()
                    lines = mark_line(lines, url, "✅ ", f"  <!-- {title} -->")
                    for img in screenshots:
                        img.unlink(missing_ok=True)
                except Exception as e:
                    jobs[job_id].update({"status": "error", "error": str(e)})
                    lines = OBSIDIAN_QUEUE.read_text(encoding="utf-8").splitlines()
                    lines = mark_line(lines, url, "❌ ", f"  <!-- {e} -->")

                OBSIDIAN_QUEUE.write_text("\n".join(lines) + "\n", encoding="utf-8")

            if changed:
                OBSIDIAN_QUEUE.write_text("\n".join(lines) + "\n", encoding="utf-8")

        except Exception:
            pass

        await asyncio.sleep(5)


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path("templates/index.html")
    return html_path.read_text()


@app.post("/api/process")
async def api_process(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    use_whisper: bool = Form(False),
    screenshots: list[UploadFile] = File(default=[]),
):
    video_id = extract_video_id(url)
    if not video_id:
        raise HTTPException(400, "Invalid YouTube URL")

    job_id = f"job_{video_id}_{int(asyncio.get_event_loop().time())}"
    jobs[job_id] = {"status": "processing", "url": url}

    # Save uploaded screenshots to disk
    saved_screenshots: list[Path] = []
    for upload in screenshots:
        if upload.filename:
            dest = UPLOADS_DIR / upload.filename
            dest.write_bytes(await upload.read())
            saved_screenshots.append(dest)

    async def run():
        try:
            result = await process_video(url, saved_screenshots, use_whisper)
            jobs[job_id].update({"status": "done", **result})
        except Exception as e:
            jobs[job_id].update({"status": "error", "error": str(e)})

    background_tasks.add_task(run)
    return {"job_id": job_id, "status": "processing"}


@app.get("/api/job/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/api/add")
async def add_url(url: str, background_tasks: BackgroundTasks):
    """Called by the iOS Shortcut: GET /api/add?url=https://youtube.com/..."""
    video_id = extract_video_id(url)
    if not video_id:
        return {"ok": False, "error": "Not a valid YouTube URL"}

    # Append to links.txt
    line = f"\n{url}"
    with open(LINKS_FILE, "a", encoding="utf-8") as f:
        f.write(line)

    return {"ok": True, "message": f"Added to queue: {url}"}


@app.get("/api/nuggets")
async def get_nuggets(category: str = "", search: str = ""):
    all_nuggets = load_all_nuggets()
    if category:
        all_nuggets = [n for n in all_nuggets if n.get("category", "").lower() == category.lower()]
    if search:
        q = search.lower()
        all_nuggets = [
            n for n in all_nuggets
            if q in n.get("title", "").lower()
            or q in n.get("summary", "").lower()
            or any(q in t.lower() for t in n.get("tags", []))
        ]
    return all_nuggets


@app.get("/api/nugget/{video_id}")
async def get_nugget(video_id: str):
    all_nuggets = load_all_nuggets()
    match = next((n for n in all_nuggets if n.get("video_id") == video_id), None)
    if not match:
        raise HTTPException(404, "Nugget not found")
    return match


@app.get("/api/jobs")
async def get_all_jobs():
    return list(jobs.values())


@app.get("/api/categories")
async def get_categories():
    all_nuggets = load_all_nuggets()
    cats: dict[str, int] = {}
    for n in all_nuggets:
        c = n.get("category", "Uncategorized")
        cats[c] = cats.get(c, 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(cats.items())]


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host=host, port=port, reload=True)
