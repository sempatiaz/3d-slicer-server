import os
import re
import shutil
import ipaddress
import socket
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
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


def slice_path(source: Path, workdir: Path) -> Path:
    suffix = source.suffix.lower()
    if suffix not in {".stl", ".obj", ".3mf", ".amf"}:
        raise HTTPException(status_code=400, detail="Desteklenen biçimler: STL, OBJ, 3MF, AMF")
    command = slicer_command()
    if command is None:
        raise HTTPException(status_code=503, detail="PrusaSlicer CLI bu imajda kurulamadı; README'deki açıklamaya bakın")
    output = workdir / "output.gcode"
    args = command + ["--load", str(PROFILE), "--export-gcode", "--output", str(output), str(source)]
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=SLICER_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Dilimleme zaman aşımına uğradı") from exc
    if completed.returncode != 0 or not output.exists():
        message = (completed.stderr or completed.stdout or "Bilinmeyen PrusaSlicer hatası")[-1500:]
        raise HTTPException(status_code=422, detail=f"Dilimleme başarısız: {message}")
    return output


def slice_model(upload: UploadFile, workdir: Path) -> Path:
    suffix = Path(upload.filename or "model.stl").suffix.lower()
    source = workdir / f"model{suffix}"
    save_upload(upload, source)
    return slice_path(source, workdir)


def download_model(url: str, filename: str, workdir: Path) -> Path:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Geçersiz file_url")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        if any(ipaddress.ip_address(item[4][0]).is_private or ipaddress.ip_address(item[4][0]).is_loopback for item in addresses):
            raise HTTPException(status_code=400, detail="Özel ağ adreslerinden dosya indirilemez")
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail="Dosya sunucusu çözümlenemedi") from exc
    suffix = Path(filename or parsed.path).suffix.lower()
    if suffix not in {".stl", ".obj", ".3mf", ".amf"}:
        raise HTTPException(status_code=400, detail="Desteklenen biçimler: STL, OBJ, 3MF, AMF")
    target = workdir / f"model{suffix}"
    request = urllib.request.Request(url, headers={"User-Agent": "3d-slicer-server/1.1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response, target.open("wb") as output:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_MB * 1024 * 1024:
                    raise HTTPException(status_code=413, detail=f"Dosya {MAX_UPLOAD_MB} MB sınırını aşıyor")
                output.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Model dosyası indirilemedi: {exc}") from exc
    return target


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


def duration_hours(value: str) -> float:
    hours = float(re.search(r"(\d+)h", value).group(1)) if re.search(r"(\d+)h", value) else 0
    minutes = float(re.search(r"(\d+)m", value).group(1)) if re.search(r"(\d+)m", value) else 0
    seconds = float(re.search(r"(\d+)s", value).group(1)) if re.search(r"(\d+)s", value) else 0
    return hours + minutes / 60 + seconds / 3600


def priced_result(result: dict, payload: dict) -> dict:
    grams = float(result.get("filament_used_g", 0))
    hours = duration_hours(str(result.get("estimated_print_time", "")))
    quantity = max(1, int(payload.get("quantity", 1)))
    material = grams / 1000 * float(payload.get("material_price_per_kg", 0))
    material *= 1 + float(payload.get("waste_percent", 0)) / 100
    machine = hours * float(payload.get("printer_hourly_cost", 0))
    labor = hours * float(payload.get("labor_hour", 0))
    subtotal = (material + machine + labor) * quantity
    selling = subtotal * (1 + float(payload.get("profit_percent", 0)) / 100)
    selling = max(selling, float(payload.get("minimum_order", 0)))
    return {
        **result,
        "filament_grams": grams * quantity,
        "print_time_text": result.get("estimated_print_time", "-"),
        "selling_price": round(selling, 2),
        "quantity": quantity,
    }


@app.post("/quote", dependencies=[Depends(require_api_key)])
async def quote(request: Request, file: UploadFile | None = File(default=None)) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        workdir = Path(directory)
        payload: dict = {}
        if file is not None:
            gcode = slice_model(file, workdir)
        else:
            try:
                payload = await request.json()
            except Exception as exc:
                raise HTTPException(status_code=422, detail="file veya JSON file_url gerekli") from exc
            source = download_model(str(payload.get("file_url", "")), str(payload.get("filename", "")), workdir)
            gcode = slice_path(source, workdir)
        result = {"mode": "real", "currency": "TRY", **metadata(gcode)}
        return priced_result(result, payload) if payload else result


@app.post("/slice", dependencies=[Depends(require_api_key)])
def download_slice(file: UploadFile = File(...)) -> FileResponse:
    # Background cleanup cannot remove the file before FileResponse sends it;
    # place it in /tmp and let the platform's ephemeral lifecycle clean it.
    directory = Path(tempfile.mkdtemp(prefix="slicer-"))
    gcode = slice_model(file, directory)
    return FileResponse(gcode, media_type="text/plain", filename="output.gcode")
