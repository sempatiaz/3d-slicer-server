import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="3D Slicer Server", version="1.0.0")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
SLICER_TIMEOUT = int(os.getenv("SLICER_TIMEOUT", "240"))
PROFILE = Path(os.getenv("SLICER_PROFILE", "/app/profiles/default.ini"))


def slicer_command() -> list[str] | None:
    binary = shutil.which("prusa-slicer") or shutil.which("PrusaSlicer")
    if not binary:
        return None
    xvfb = shutil.which("xvfb-run")
    return ([xvfb, "-a", binary] if xvfb else [binary])


def require_api_key(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    expected = os.getenv("API_KEY", "").strip()
    if not expected:
        return
    bearer = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
    if x_api_key != expected and bearer != expected:
        raise HTTPException(status_code=401, detail="Geçersiz veya eksik API anahtarı")


class Health(BaseModel):
    status: str
    slicer_available: bool
    slicer_mode: str


@app.get("/health", response_model=Health)
def health() -> Health:
    available = slicer_command() is not None
    return Health(status="ok", slicer_available=available, slicer_mode="real" if available else "unavailable")


def save_upload(upload: UploadFile, target: Path) -> None:
    size = 0
    with target.open("wb") as output:
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                raise HTTPException(status_code=413, detail=f"Dosya {MAX_UPLOAD_MB} MB sınırını aşıyor")
            output.write(chunk)


def slice_model(upload: UploadFile, workdir: Path) -> Path:
    suffix = Path(upload.filename or "model.stl").suffix.lower()
    if suffix not in {".stl", ".obj", ".3mf", ".amf"}:
        raise HTTPException(status_code=400, detail="Desteklenen biçimler: STL, OBJ, 3MF, AMF")
    command = slicer_command()
    if command is None:
        raise HTTPException(status_code=503, detail="PrusaSlicer CLI bu imajda kurulamadı; README'deki açıklamaya bakın")
    source = workdir / f"model{suffix}"
    output = workdir / "output.gcode"
    save_upload(upload, source)
    args = command + ["--load", str(PROFILE), "--export-gcode", "--output", str(output), str(source)]
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=SLICER_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Dilimleme zaman aşımına uğradı") from exc
    if completed.returncode != 0 or not output.exists():
        message = (completed.stderr or completed.stdout or "Bilinmeyen PrusaSlicer hatası")[-1500:]
        raise HTTPException(status_code=422, detail=f"Dilimleme başarısız: {message}")
    return output


def metadata(gcode: Path) -> dict:
    text = gcode.read_text(encoding="utf-8", errors="ignore")
    patterns = {
        "estimated_print_time": r"estimated printing time \(normal mode\) = (.+)",
        "filament_used_mm": r"filament used \[mm\] = ([\d.]+)",
        "filament_used_g": r"filament used \[g\] = ([\d.]+)",
        "total_cost": r"total filament cost = ([\d.]+)",
    }
    result: dict[str, str | float | int] = {"gcode_bytes": gcode.stat().st_size}
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            result[key] = float(value) if key != "estimated_print_time" else value
    return result


@app.post("/quote", dependencies=[Depends(require_api_key)])
def quote(file: UploadFile = File(...)) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        gcode = slice_model(file, Path(directory))
        return {"mode": "real", "currency": "profile-defined", **metadata(gcode)}


@app.post("/slice", dependencies=[Depends(require_api_key)])
def download_slice(file: UploadFile = File(...)) -> FileResponse:
    # Background cleanup cannot remove the file before FileResponse sends it;
    # place it in /tmp and let the platform's ephemeral lifecycle clean it.
    directory = Path(tempfile.mkdtemp(prefix="slicer-"))
    gcode = slice_model(file, directory)
    return FileResponse(gcode, media_type="text/plain", filename="output.gcode")
