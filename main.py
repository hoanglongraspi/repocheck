# main.py (Coolify-friendly)
from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.middleware.proxy_headers import ProxyHeadersMiddleware
import json, os, re
from datetime import datetime
from typing import Optional

app = FastAPI()

# ✅ Important for Coolify / reverse proxy (Traefik/Nginx):
# Makes request.base_url and client IP behave correctly with X-Forwarded-* headers.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# ---------------------------
# Config (Coolify-friendly)
# ---------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# You should mount a persistent volume to this path in Coolify:
#   Host: /data/thingx/audio  -> Container: /app/audio/uploads
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "audio", "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Optional: set PUBLIC_BASE_URL in Coolify env to force returned URLs (recommended).
# Example: https://api.example.com
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

print(f"[STARTUP] BASE_DIR   = {BASE_DIR}")
print(f"[STARTUP] UPLOAD_DIR = {UPLOAD_DIR}")
if PUBLIC_BASE_URL:
    print(f"[STARTUP] PUBLIC_BASE_URL = {PUBLIC_BASE_URL}")


def ts_to_local_datetime(ts):
    """
    Convert Unix timestamp to local datetime (server timezone).
    Accepts seconds or milliseconds.
    """
    try:
        ts_int = int(ts)
    except Exception:
        raise HTTPException(400, "startTime/endTime must be numeric")

    # Heuristic: >= 1e12 likely milliseconds
    if ts_int >= 1_000_000_000_000:
        ts_sec = ts_int / 1000.0
    else:
        ts_sec = float(ts_int)

    return datetime.fromtimestamp(ts_sec)  # server local timezone


# ---------------------------
# Filename parsing for GET /list
# Expected filename format:
#   {startStr}_{endStr}_{macClean}_{orig_name}
# Example:
#   20260204_133555_069_20260204_133655_619_4CFF01A007C2_foo.wav
# ---------------------------
FILENAME_RE = re.compile(
    r"^(?P<start>\d{8}_\d{6}_\d{3})_"
    r"(?P<end>\d{8}_\d{6}_\d{3})_"
    r"(?P<mac>[A-Fa-f0-9]+)_(?P<rest>.+)$"
)


def parse_filename(filename: str):
    m = FILENAME_RE.match(filename)
    if not m:
        return None

    def parse_ms_dt(s: str) -> datetime:
        # s format: YYYYMMDD_HHMMSS_mmm
        date_part, time_part, ms_part = s.split("_")
        us = int(ms_part) * 1000
        return datetime.strptime(f"{date_part}_{time_part}_{us:06d}", "%Y%m%d_%H%M%S_%f")

    start_dt = parse_ms_dt(m.group("start"))
    end_dt = parse_ms_dt(m.group("end"))

    return {
        "filename": filename,
        "mac": m.group("mac"),
        "startLocal": start_dt,
        "endLocal": end_dt,
        "rest": m.group("rest"),
    }


def guess_media_type(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".wav"):
        return "audio/wav"
    if lower.endswith(".mp3"):
        return "audio/mpeg"
    if lower.endswith(".m4a"):
        return "audio/mp4"
    if lower.endswith(".aac"):
        return "audio/aac"
    return "application/octet-stream"


def get_base_url(request: Request) -> str:
    """
    Prefer PUBLIC_BASE_URL if provided (best for production),
    else derive from request (works with ProxyHeadersMiddleware).
    """
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    return str(request.base_url).rstrip("/")


@app.post("/thingx/api/file/upload/audio")
async def upload_audio(
    file: UploadFile = File(...),
    metadata: UploadFile = File(...)
):
    try:
        metadata_content = await metadata.read()
        metadata_str = metadata_content.decode("utf-8", errors="strict")
        print(f"[DEBUG] raw metadata: {metadata_str}")

        meta = json.loads(metadata_str)

        required = ["userId", "name", "startTime", "endTime", "mac", "size"]
        for field in required:
            if field not in meta:
                raise HTTPException(400, f"Missing field: {field}")

        orig_name = os.path.basename(str(meta["name"]))
        mac_clean = str(meta["mac"]).replace(":", "").replace("-", "")

        start_dt = ts_to_local_datetime(meta["startTime"])
        end_dt = ts_to_local_datetime(meta["endTime"])

        # safe filename format: YYYYMMDD_HHMMSS_mmm
        start_str = start_dt.strftime("%Y%m%d_%H%M%S_%f")[:-3]
        end_str = end_dt.strftime("%Y%m%d_%H%M%S_%f")[:-3]

        final_filename = f"{start_str}_{end_str}_{mac_clean}_{orig_name}"
        save_path = os.path.join(UPLOAD_DIR, final_filename)

        audio_bytes = await file.read()
        print(f"[DEBUG] will save to: {save_path}")
        print(f"[DEBUG] received bytes: {len(audio_bytes)}")
        print(f"[DEBUG] recording time (local): start={start_dt}, end={end_dt}")

        with open(save_path, "wb") as f:
            f.write(audio_bytes)
            f.flush()
            os.fsync(f.fileno())

        size_on_disk = os.path.getsize(save_path)

        print(
            f"[{datetime.now()}] "
            f"Saved {final_filename} from {meta['mac']} ({size_on_disk} bytes)"
        )

        return {
            "code": 200,
            "message": "success",
            "data": {
                "filename": final_filename,
                "size": size_on_disk,
                "recording_local": {
                    "start": start_dt.isoformat(sep=" ", timespec="milliseconds"),
                    "end": end_dt.isoformat(sep=" ", timespec="milliseconds"),
                }
            }
        }

    except UnicodeDecodeError:
        raise HTTPException(400, "Metadata is not valid UTF-8 text")
    except json.JSONDecodeError:
        raise HTTPException(400, "Metadata is not valid JSON")
    except HTTPException:
        raise
    except Exception as e:
        print("[ERROR] Server error:", repr(e))
        raise HTTPException(500, f"Internal server error: {type(e).__name__}")


@app.get("/thingx/api/audio/list")
def list_audio(
    request: Request,
    mac: Optional[str] = None,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    limit: int = 500
):
    """
    List saved audio files in UPLOAD_DIR.
    Filters:
      - mac: only return matching mac (case-insensitive)
      - from_time / to_time: ISO datetime string, server local time
        e.g. "2026-02-04 13:00:00" or "2026-02-04T13:00:00"
    """
    try:
        from_dt = datetime.fromisoformat(from_time) if from_time else None
        to_dt = datetime.fromisoformat(to_time) if to_time else None
    except Exception:
        raise HTTPException(400, "from_time/to_time must be ISO datetime")

    base_url = get_base_url(request)

    items = []
    for fname in os.listdir(UPLOAD_DIR):
        full_path = os.path.join(UPLOAD_DIR, fname)
        if not os.path.isfile(full_path):
            continue

        info = parse_filename(fname)
        if not info:
            continue

        if mac and info["mac"].lower() != mac.lower():
            continue
        if from_dt and info["endLocal"] < from_dt:
            continue
        if to_dt and info["startLocal"] > to_dt:
            continue

        size = os.path.getsize(full_path)

        items.append({
            "_start_dt": info["startLocal"],   # internal for sorting
            "id": fname,
            "filename": fname,
            "mac": info["mac"],
            "startLocal": info["startLocal"].isoformat(sep=" ", timespec="milliseconds"),
            "endLocal": info["endLocal"].isoformat(sep=" ", timespec="milliseconds"),
            "size": size,
            "contentType": guess_media_type(fname),
            "url": f"{base_url}/thingx/api/audio/file/{fname}",
        })

    # Sort by actual datetime
    items.sort(key=lambda x: x["_start_dt"])
    # remove internal key
    for it in items:
        it.pop("_start_dt", None)

    return {"code": 200, "count": len(items), "data": items[: max(1, limit)]}


@app.get("/thingx/api/audio/file/{filename}")
def get_audio_file(filename: str):
    safe_name = os.path.basename(filename)
    file_path = os.path.join(UPLOAD_DIR, safe_name)

    if not os.path.exists(file_path):
        raise HTTPException(404, "File not found")

    media_type = guess_media_type(safe_name)
    return FileResponse(file_path, media_type=media_type, filename=safe_name)


@app.get("/thingx/api/audio/health")
def health():
    file_count = len(
        [f for f in os.listdir(UPLOAD_DIR) if os.path.isfile(os.path.join(UPLOAD_DIR, f))]
    )
    return {
        "code": 200,
        "status": "ok",
        "upload_dir": UPLOAD_DIR,
        "files": file_count,
        "public_base_url": PUBLIC_BASE_URL or None,
    }
