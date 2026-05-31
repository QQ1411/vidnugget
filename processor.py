"""Core processing logic for VidNugget."""

import os
import re
import json
import base64
import asyncio
from pathlib import Path
from datetime import datetime

import anthropic
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled


def slugify(text: str, max_length: int = 60, separator: str = "_") -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s-]+", separator, text).strip(separator)
    return text[:max_length]

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# iCloud Drive paths — readable/writable from iPhone via Files app
_ICLOUD = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs"
ICLOUD_ROOT = _ICLOUD / "VidNugget"
INBOX_DIR = ICLOUD_ROOT / "inbox"
KNOWLEDGE_BASE = ICLOUD_ROOT / "knowledge_base"

for _d in (INBOX_DIR, KNOWLEDGE_BASE, INBOX_DIR / "done"):
    _d.mkdir(parents=True, exist_ok=True)

# Keep a local uploads dir as fallback for the web UI
UPLOADS_DIR = Path("uploads")
UPLOADS_DIR.mkdir(exist_ok=True)


def extract_video_id(url: str) -> str | None:
    patterns = [
        r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def get_youtube_transcript(video_id: str) -> tuple[str, str]:
    """Returns (transcript_text, method_used). Supports youtube-transcript-api v1.x."""
    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id)
        text = " ".join(s.text for s in fetched.snippets)
        return text, "youtube_captions"
    except (NoTranscriptFound, TranscriptsDisabled):
        pass

    # Try any available language
    try:
        transcript_list = api.list(video_id)
        for t in transcript_list:
            try:
                fetched = t.fetch()
                text = " ".join(s.text for s in fetched.snippets)
                return text, f"youtube_captions_{t.language}"
            except Exception:
                continue
    except Exception:
        pass

    raise RuntimeError("No captions available. Enable Whisper fallback or choose a video with captions.")


def get_transcript_with_whisper(video_id: str) -> tuple[str, str]:
    """Download audio and transcribe with Whisper (slow but thorough)."""
    import whisper
    import yt_dlp

    audio_path = UPLOADS_DIR / f"{video_id}.mp3"
    url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(UPLOADS_DIR / f"{video_id}.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    model_name = os.environ.get("WHISPER_MODEL", "base")
    model = whisper.load_model(model_name)
    result = model.transcribe(str(audio_path))
    audio_path.unlink(missing_ok=True)
    return result["text"], f"whisper_{model_name}"


def get_video_title(video_id: str) -> str:
    """Fetch video title via yt-dlp (no download)."""
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            return info.get("title", video_id)
    except Exception:
        return video_id


def encode_image(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


def analyze_screenshots(screenshot_paths: list[Path]) -> str:
    """Use Claude Vision to extract text/context from screenshots."""
    if not screenshot_paths:
        return ""

    content = [{"type": "text", "text": "These are screenshots from or related to a video. Extract all visible text, key concepts, diagrams, or important information shown. Be thorough."}]
    for path in screenshot_paths[:8]:  # cap at 8 images
        ext = path.suffix.lower().lstrip(".")
        media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": encode_image(path)},
        })

    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=2000,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


def generate_nugget(title: str, transcript: str, screenshot_context: str) -> dict:
    """Ask Claude to categorize, summarize, and create a knowledge nugget."""
    combined = transcript
    if screenshot_context:
        combined += f"\n\n[SCREENSHOT CONTEXT]\n{screenshot_context}"

    # Truncate transcript to ~80k chars to stay within context limits
    if len(combined) > 80000:
        combined = combined[:80000] + "\n\n[transcript truncated]"

    prompt = f"""You are processing a YouTube video for a personal knowledge base.

Video title: {title}

Content (transcript + any screenshot context):
{combined}

Produce a JSON response with exactly these fields:
{{
  "category": "A short 1-3 word category (e.g. 'AI Tools', 'Productivity', 'Finance', 'Cooking', 'Programming', etc.)",
  "tags": ["tag1", "tag2", "tag3"],
  "summary": "2-3 sentence plain-English summary of what the video covers",
  "key_points": ["Point 1", "Point 2", "Point 3", "Point 4", "Point 5"],
  "knowledge_nugget": "A rich, structured markdown note (200-400 words) that captures the most valuable, actionable, and memorable insights. Use headers, bullet points, and bold for key terms. Written so you can recall the value of this video months later without re-watching.",
  "action_items": ["Specific thing to try or do based on this content"],
  "rating": "1-10 score for how valuable/insightful this content is, as integer"
}}

Return only valid JSON, no markdown fences."""

    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    # Strip any accidental markdown fences
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def save_nugget(video_id: str, title: str, url: str, transcript: str,
                nugget: dict, screenshot_paths: list[Path], method: str) -> Path:
    """Save everything to knowledge_base/{category}/{slug}/"""
    category = slugify(nugget["category"], separator="_")
    slug = slugify(title, max_length=60) or video_id
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = KNOWLEDGE_BASE / category / f"{slug}_{timestamp}"
    folder.mkdir(parents=True, exist_ok=True)

    # Raw transcript
    (folder / "transcript.txt").write_text(transcript, encoding="utf-8")

    # Knowledge nugget markdown
    nugget_md = f"""# {title}

**URL:** {url}
**Category:** {nugget['category']}
**Tags:** {', '.join(nugget['tags'])}
**Rating:** {nugget['rating']}/10
**Processed:** {datetime.now().strftime('%Y-%m-%d %H:%M')}
**Transcript method:** {method}

---

## Summary
{nugget['summary']}

---

## Key Points
{chr(10).join(f'- {p}' for p in nugget['key_points'])}

---

## Knowledge Nugget
{nugget['knowledge_nugget']}

---

## Action Items
{chr(10).join(f'- [ ] {a}' for a in nugget['action_items'])}
"""
    (folder / "nugget.md").write_text(nugget_md, encoding="utf-8")

    # Copy screenshots into folder
    if screenshot_paths:
        ss_dir = folder / "screenshots"
        ss_dir.mkdir(exist_ok=True)
        import shutil
        for p in screenshot_paths:
            shutil.copy2(p, ss_dir / p.name)

    # Metadata JSON for the UI
    meta = {
        "video_id": video_id,
        "title": title,
        "url": url,
        "category": nugget["category"],
        "tags": nugget["tags"],
        "summary": nugget["summary"],
        "rating": nugget["rating"],
        "folder": str(folder),
        "processed_at": datetime.now().isoformat(),
    }
    (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return folder


async def process_video(url: str, screenshot_paths: list[Path] | None = None,
                        use_whisper: bool = False) -> dict:
    """Full pipeline: URL → nugget saved to disk. Returns result dict."""
    screenshot_paths = screenshot_paths or []

    video_id = extract_video_id(url)
    if not video_id:
        raise ValueError(f"Could not extract video ID from URL: {url}")

    # Get title
    title = await asyncio.get_event_loop().run_in_executor(None, get_video_title, video_id)

    # Get transcript
    if use_whisper:
        transcript, method = await asyncio.get_event_loop().run_in_executor(
            None, get_transcript_with_whisper, video_id
        )
    else:
        transcript, method = await asyncio.get_event_loop().run_in_executor(
            None, get_youtube_transcript, video_id
        )

    # Analyze screenshots
    screenshot_context = ""
    if screenshot_paths:
        screenshot_context = await asyncio.get_event_loop().run_in_executor(
            None, analyze_screenshots, screenshot_paths
        )

    # Generate nugget
    nugget = await asyncio.get_event_loop().run_in_executor(
        None, generate_nugget, title, transcript, screenshot_context
    )

    # Save to disk
    folder = await asyncio.get_event_loop().run_in_executor(
        None, save_nugget, video_id, title, url, transcript, nugget, screenshot_paths, method
    )

    return {
        "success": True,
        "title": title,
        "video_id": video_id,
        "category": nugget["category"],
        "folder": str(folder),
        "nugget": nugget,
        "transcript_method": method,
    }


def load_all_nuggets() -> list[dict]:
    """Walk knowledge_base and load all meta.json files."""
    nuggets = []
    for meta_file in sorted(KNOWLEDGE_BASE.rglob("meta.json"), reverse=True):
        try:
            data = json.loads(meta_file.read_text())
            nugget_file = meta_file.parent / "nugget.md"
            data["nugget_md"] = nugget_file.read_text() if nugget_file.exists() else ""
            nuggets.append(data)
        except Exception:
            continue
    return nuggets
