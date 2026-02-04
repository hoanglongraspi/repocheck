from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import FileResponse
import json, os, re
from datetime import datetime
from typing import Optional

# =========================
# App
# =========================
app = FastAPI(
    title="ThingX Audio API (Demo)",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# =========================
# Config
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Coolify: mount volume vào path này
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "audio", "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

# (Optional) set trong Coolify env để URL trả về đúng domain
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

print(f"[STARTUP] BASE_DIR   = {BASE_DIR}")
print(f"[STARTUP] UPLOAD_DIR = {UPLOAD_DIR}")
if PUBLIC_BASE_URL:
    print(f"[STARTUP] PUBLIC_BASE_URL = {PUBLIC_BASE_URL}")

# =========================
# Utils
# =========================
def ts_to_local_datetime(ts):
    """
    Convert Unix timestamp (sec or ms) to server local datetime
    """
    try:
        ts_int = int(ts)
    except Exception:
        raise HTTPException(400, "startTime/endTime must be numeric")

    if ts_int >= 1_000_000_000_000:  # ms
        ts_sec = ts_int / 1000.0
    else:
        ts_sec = float(ts_int)

    return datetime.fromtimestamp(ts_sec)


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
        date_part, time_part, ms_part = s.split("_")
        us = int(ms_part) * 1000
        return datetime.strptime(
            f"{date_part}_{time_part}_{us:06d}",
            "%Y%m%d_%H%M%S_%f",
        )

    return {
        "filename": filename,
        "mac": m.group("mac"),
        "startLocal": parse_ms_dt(m.group("start")),
        "endLocal": parse_ms_dt(m.group("end")),
        "rest": m.group("rest"),
    }


def guess_media_type(filename: str) -> str:
    f = filename.lower()
    if f.endswith(".wav"):
        return "audio/wav"
    if f.endswith(".mp3"):
        return "audio/mpeg"
    if f.endswith(".m4a"):
        return "audio/mp4"
    if f.endswith(".aac"):
        return "audio/aac"
    return "application/octet-stream"


def get_base_url(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    return str(request.base_url).rstrip("/")

# =========================
# APIs
# =========================
@app.post("/thingx/api/file/upload/audio")
async def upload_audio(
    file: UploadFile = File(...),
    metadata: UploadFile = File(...)
):
    try:
        meta = json.loads((await metadata.read()).decode("utf-8"))

        for k in ["userId", "name", "startTime", "endTime", "mac", "size"]:
            if k not in meta:
                raise HTTPException(400, f"Missing field: {k}")

        orig_name = os.path.basename(str(meta["name"]))
        mac_clean = str(meta["mac"]).replace(":", "").replace("-", "")

        start_dt = ts_to_local_datetime(meta["startTime"])
        end_dt = ts_to_local_datetime(meta["endTime"])

        start_str = start_dt.strftime("%Y%m%d_%H%M%S_%f")[:-3]
        end_str = end_dt.strftime("%Y%m%d_%H%M%S_%f")[:-3]

        final_filename = f"{start_str}_{end_str}_{mac_clean}_{orig_name}"
        save_path = os.path.join(UPLOAD_DIR, final_filename)

        audio_bytes = await file.read()
        with open(save_path, "wb") as f:
            f.write(audio_bytes)

        return {
            "code": 200,
            "message": "success",
            "data": {
                "filename": final_filename,
                "size": os.path.getsize(save_path),
                "recording_local": {
                    "start": start_dt.isoformat(sep=" ", timespec="milliseconds"),
                    "end": end_dt.isoformat(sep=" ", timespec="milliseconds"),
                }
            }
        }

    except json.JSONDecodeError:
        raise HTTPException(400, "Metadata is not valid JSON")
    except Exception as e:
        print("[ERROR]", repr(e))
        raise HTTPException(500, "Internal server error")


@app.get("/thingx/api/audio/list")
def list_audio(
    request: Request,
    mac: Optional[str] = None,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    limit: int = 500
):
    try:
        from_dt = datetime.fromisoformat(from_time) if from_time else None
        to_dt = datetime.fromisoformat(to_time) if to_time else None
    except Exception:
        raise HTTPException(400, "from_time/to_time must be ISO datetime")

    base_url = get_base_url(request)
    items = []

    for fname in os.listdir(UPLOAD_DIR):
        path = os.path.join(UPLOAD_DIR, fname)
        if not os.path.isfile(path):
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

        items.append({
            "_sort": info["startLocal"],
            "filename": fname,
            "mac": info["mac"],
            "startLocal": info["startLocal"].isoformat(sep=" ", timespec="milliseconds"),
            "endLocal": info["endLocal"].isoformat(sep=" ", timespec="milliseconds"),
            "size": os.path.getsize(path),
            "url": f"{base_url}/thingx/api/audio/file/{fname}",
            "contentType": guess_media_type(fname),
        })

    items.sort(key=lambda x: x["_sort"])
    for i in items:
        i.pop("_sort", None)

    return {"code": 200, "count": len(items), "data": items[: max(1, limit)]}


@app.get("/thingx/api/audio/file/{filename}")
def get_audio_file(filename: str):
    safe_name = os.path.basename(filename)
    path = os.path.join(UPLOAD_DIR, safe_name)

    if not os.path.exists(path):
        raise HTTPException(404, "File not found")

    return FileResponse(
        path,
        media_type=guess_media_type(safe_name),
        filename=safe_name,
    )


@app.get("/thingx/api/audio/health")
def health():
    return {
        "code": 200,
        "status": "ok",
        "files": len([
            f for f in os.listdir(UPLOAD_DIR)
            if os.path.isfile(os.path.join(UPLOAD_DIR, f))
        ]),
    }
