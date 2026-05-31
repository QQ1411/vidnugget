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
    INBOX_DIR, extract_video_id
)

# In-memory job tracker
jobs: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start folder watcher for drop-in URL files
    asyncio.create_task(watch_inbox())
    yield

app = FastAPI(title="VidNugget", lifespan=lifespan)


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


async def watch_inbox():
    """
    Watch iCloud inbox/ for .txt files containing a YouTube URL.

    Workflow the user follows on iPhone:
      1. Create a plain .txt file with a YouTube URL (one line is enough).
         Name it anything, e.g. 'my_video.txt'.
      2. Optionally drop screenshot images (.jpg/.png) into inbox/ alongside it.
      3. Save/AirDrop everything into iCloud Drive → VidNugget → inbox/
      4. VidNugget picks them up within 5 seconds, processes, and saves the
         nugget to iCloud Drive → VidNugget → knowledge_base/

    After processing, the .txt is moved to inbox/done/ and images are deleted.
    """
    processed: set[Path] = set()
    while True:
        for txt_file in INBOX_DIR.glob("*.txt"):
            if txt_file in processed or txt_file.parent.name == "done":
                continue
            processed.add(txt_file)
            try:
                content = txt_file.read_text(encoding="utf-8").strip()
                url = next(
                    (l.strip() for l in content.splitlines()
                     if "youtube.com" in l or "youtu.be" in l),
                    None,
                )
                if not url:
                    continue

                # Grab all images currently sitting in inbox/ (user's screenshots)
                screenshots = [
                    p for p in INBOX_DIR.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_EXTS
                ]

                job_id = f"inbox_{txt_file.stem}_{extract_video_id(url) or 'unknown'}"
                jobs[job_id] = {"status": "processing", "url": url, "source": "icloud_inbox"}
                try:
                    result = await process_video(url, screenshots)
                    jobs[job_id].update({"status": "done", **result})
                except Exception as e:
                    jobs[job_id].update({"status": "error", "error": str(e)})

                # Move .txt to done/, remove processed images
                txt_file.rename(INBOX_DIR / "done" / txt_file.name)
                for img in screenshots:
                    img.unlink(missing_ok=True)

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
