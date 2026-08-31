import os
import re
import shutil
import ipaddress
import socket
import subprocess
import tempfile
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

app = FastAPI(title="3D Slicer Server", version="1.1.0")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
SLICER_TIMEOUT = int(os.getenv("SLICER_TIMEOUT", "240"))
PROFILE = Path(os.getenv("SLICER_PROFILE", "/app/profiles/default.ini"))
SUPPORTED_SUFFIXES = {".stl", ".obj", ".3mf", ".amf"}
LOG_LIMIT = 12000


class QuoteError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        error_type: str,
        *,
        detail: str | None = None,
        stdout: str | bytes | None = None,
        stderr: str | bytes | None = None,
        exit_code: int | None = None,
        validation_reason: Any = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.detail = detail or message
        self.stdout = clean_process_output(stdout)
        self.stderr = clean_process_output(stderr)
        self.exit_code = exit_code
        self.validation_reason = validation_reason
        super().__init__(self.detail)


def clean_process_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value.strip()[-LOG_LIMIT:]


def error_payload(exc: QuoteError) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message": exc.message,
        "detail": exc.detail,
        "error_type": exc.error_type,
    }
    if exc.validation_reason is not None:
        payload["validation_reason"] = exc.validation_reason
    if exc.exit_code is not None:
        payload["exit_code"] = exc.exit_code
    if exc.stdout:
        payload["prusa_stdout"] = exc.stdout
    if exc.stderr:
        payload["prusa_stderr"] = exc.stderr
    return payload


@app.exception_handler(QuoteError)
async def quote_error_handler(_: Request, exc: QuoteError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=error_payload(exc))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    if request.url.path != "/quote":
        return JSONResponse(status_code=422, content={"detail": exc.errors()})
    error = QuoteError(
        422,
        "İstek doğrulanamadı",
        "request_validation_error",
        detail="/quote isteğindeki alanlardan biri eksik veya geçersiz.",
        validation_reason=jsonable_encoder(exc.errors()),
    )
    return JSONResponse(status_code=422, content=error_payload(error))


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if request.url.path != "/quote":
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
    error_type = "authentication_error" if exc.status_code in {401, 403} else "http_error"
    error = QuoteError(exc.status_code, str(exc.detail), error_type, detail=str(exc.detail))
    return JSONResponse(status_code=exc.status_code, content=error_payload(error), headers=exc.headers)


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


def save_upload(upload: UploadFile, target: Path, *, quote_errors: bool = False) -> None:
    size = 0
    with target.open("wb") as output:
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                if quote_errors:
                    raise QuoteError(413, "Dosya çok büyük", "file_too_large", detail=f"Dosya {MAX_UPLOAD_MB} MB sınırını aşıyor")
                raise HTTPException(status_code=413, detail=f"Dosya {MAX_UPLOAD_MB} MB sınırını aşıyor")
            output.write(chunk)


def slice_path(source: Path, workdir: Path, *, quote_errors: bool = False) -> Path:
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        if quote_errors:
            raise QuoteError(415, "Dosya tipi desteklenmiyor", "unsupported_file_type", detail="Desteklenen biçimler: STL, OBJ, 3MF, AMF", validation_reason={"suffix": suffix or None})
        raise HTTPException(status_code=400, detail="Desteklenen biçimler: STL, OBJ, 3MF, AMF")
    command = slicer_command()
    if command is None:
        if quote_errors:
            raise QuoteError(503, "PrusaSlicer kullanılamıyor", "slicer_unavailable", detail="PrusaSlicer CLI bu imajda bulunamadı")
        raise HTTPException(status_code=503, detail="PrusaSlicer CLI bu imajda kurulamadı; README'deki açıklamaya bakın")
    output = workdir / "output.gcode"
    args = command + ["--load", str(PROFILE), "--export-gcode", "--output", str(output), str(source)]
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=SLICER_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        if quote_errors:
            raise QuoteError(504, "Dilimleme zaman aşımına uğradı", "slicer_timeout", detail=f"PrusaSlicer {SLICER_TIMEOUT} saniye içinde tamamlanmadı.", stdout=exc.stdout, stderr=exc.stderr) from exc
        raise HTTPException(status_code=504, detail="Dilimleme zaman aşımına uğradı") from exc
    if completed.returncode != 0 or not output.exists():
        message = (completed.stderr or completed.stdout or "Bilinmeyen PrusaSlicer hatası")[-1500:]
        if quote_errors:
            combined = f"{completed.stdout}\n{completed.stderr}".lower()
            bed_patterns = ("outside the print area", "outside print area", "does not fit", "too large for", "bed shape", "print bed")
            if any(pattern in combined for pattern in bed_patterns):
                raise QuoteError(422, "Model baskı alanına sığmıyor", "model_outside_print_area", detail="PrusaSlicer modelin baskı tablası sınırlarının dışında olduğunu bildirdi.", stdout=completed.stdout, stderr=completed.stderr, exit_code=completed.returncode, validation_reason="model_outside_print_area")
            raise QuoteError(422, "PrusaSlicer modeli dilimleyemedi", "slicer_exit_error", detail=f"PrusaSlicer başarısız çıkış kodu döndürdü: {completed.returncode}", stdout=completed.stdout, stderr=completed.stderr, exit_code=completed.returncode)
        raise HTTPException(status_code=422, detail=f"Dilimleme başarısız: {message}")
    return output


def slice_model(upload: UploadFile, workdir: Path, *, quote_errors: bool = False) -> Path:
    suffix = Path(upload.filename or "model.stl").suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES and quote_errors:
        raise QuoteError(415, "Dosya tipi desteklenmiyor", "unsupported_file_type", detail="Desteklenen biçimler: STL, OBJ, 3MF, AMF", validation_reason={"filename": upload.filename, "suffix": suffix or None})
    source = workdir / f"model{suffix}"
    save_upload(upload, source, quote_errors=quote_errors)
    return slice_path(source, workdir, quote_errors=quote_errors)


def download_model(url: str, filename: str, workdir: Path) -> Path:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise QuoteError(400, "Dosya adresi geçersiz", "file_download_error", detail="file_url geçerli bir HTTP veya HTTPS adresi olmalı.", validation_reason="invalid_file_url")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        if any(ipaddress.ip_address(item[4][0]).is_private or ipaddress.ip_address(item[4][0]).is_loopback for item in addresses):
            raise QuoteError(400, "Dosya adresine erişilemiyor", "file_download_error", detail="Özel ağ adreslerinden dosya indirilemez.", validation_reason="private_network_url")
    except socket.gaierror as exc:
        raise QuoteError(422, "Dosya indirilemiyor", "file_download_error", detail="Dosya sunucusunun adresi çözümlenemedi.", validation_reason="dns_resolution_failed") from exc
    suffix = Path(filename or parsed.path).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise QuoteError(415, "Dosya tipi desteklenmiyor", "unsupported_file_type", detail="Desteklenen biçimler: STL, OBJ, 3MF, AMF", validation_reason={"filename": filename or parsed.path, "suffix": suffix or None})
    target = workdir / f"model{suffix}"
    request = urllib.request.Request(url, headers={"User-Agent": "3d-slicer-server/1.1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response, target.open("wb") as output:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_MB * 1024 * 1024:
                    raise QuoteError(413, "Dosya çok büyük", "file_too_large", detail=f"Dosya {MAX_UPLOAD_MB} MB sınırını aşıyor")
                output.write(chunk)
    except QuoteError:
        raise
    except urllib.error.HTTPError as exc:
        raise QuoteError(422, "Dosya indirilemiyor", "file_download_error", detail=f"Dosya sunucusu HTTP {exc.code} döndürdü.", validation_reason={"http_status": exc.code, "reason": str(exc.reason)}) from exc
    except urllib.error.URLError as exc:
        raise QuoteError(422, "Dosya indirilemiyor", "file_download_error", detail=f"Dosya sunucusuna bağlanılamadı: {exc.reason}", validation_reason="connection_failed") from exc
    except Exception as exc:
        raise QuoteError(422, "Dosya indirilemiyor", "file_download_error", detail=f"Model dosyası indirilemedi: {exc}", validation_reason=type(exc).__name__) from exc
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
    try:
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            payload: dict = {}
            if file is not None:
                gcode = slice_model(file, workdir, quote_errors=True)
            else:
                try:
                    payload = await request.json()
                except Exception as exc:
                    raise QuoteError(422, "İstek doğrulanamadı", "request_validation_error", detail="Multipart file veya JSON file_url gerekli.", validation_reason="missing_file_or_json") from exc
                if not isinstance(payload, dict) or not payload.get("file_url"):
                    raise QuoteError(422, "İstek doğrulanamadı", "request_validation_error", detail="JSON gövdesinde file_url alanı gerekli.", validation_reason="missing_file_url")
                source = download_model(str(payload["file_url"]), str(payload.get("filename", "")), workdir)
                gcode = slice_path(source, workdir, quote_errors=True)
            result = {"mode": "real", "currency": "TRY", **metadata(gcode)}
            return priced_result(result, payload) if payload else result
    except QuoteError:
        raise
    except Exception as exc:
        raise QuoteError(500, "Beklenmeyen sunucu hatası", "unexpected_error", detail=f"{type(exc).__name__}: {exc}") from exc


@app.post("/slice", dependencies=[Depends(require_api_key)])
def download_slice(file: UploadFile = File(...)) -> FileResponse:
    # Background cleanup cannot remove the file before FileResponse sends it;
    # place it in /tmp and let the platform's ephemeral lifecycle clean it.
    directory = Path(tempfile.mkdtemp(prefix="slicer-"))
    gcode = slice_model(file, directory)
    return FileResponse(gcode, media_type="text/plain", filename="output.gcode")
