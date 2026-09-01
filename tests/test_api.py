import io
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main


client = TestClient(main.app)


def test_health_contract_is_unchanged() -> None:
    with patch.object(main, "slicer_command", return_value=["prusa-slicer"]):
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "slicer_available": True, "slicer_mode": "real"}


def test_quote_rejects_unsupported_file_with_wordpress_fields() -> None:
    response = client.post("/quote", files={"file": ("model.txt", io.BytesIO(b"test"), "text/plain")})
    assert response.status_code == 415
    assert response.json()["error_type"] == "unsupported_file_type"
    assert {"message", "detail", "error_type"} <= response.json().keys()


def test_quote_reports_missing_file_url() -> None:
    response = client.post("/quote", json={})
    assert response.status_code == 422
    assert response.json()["error_type"] == "request_validation_error"
    assert response.json()["validation_reason"] == "missing_file_url"


def test_quote_auth_error_uses_wordpress_fields() -> None:
    with patch.dict(os.environ, {"API_KEY": "secret"}):
        response = client.post("/quote", json={"file_url": "https://example.com/model.stl"})
    assert response.status_code == 401
    assert response.json()["error_type"] == "authentication_error"
    assert {"message", "detail", "error_type"} <= response.json().keys()


def test_prusaslicer_exit_code_and_output_are_returned() -> None:
    completed = subprocess.CompletedProcess(["prusa-slicer"], 7, stdout="stdout örneği", stderr="mesh error")
    with tempfile.TemporaryDirectory() as directory, patch.object(main, "slicer_command", return_value=["prusa-slicer"]), patch.object(main.subprocess, "run", return_value=completed):
        try:
            main.slice_path(Path(directory) / "model.stl", Path(directory), quote_errors=True)
        except main.QuoteError as exc:
            payload = main.error_payload(exc)
        else:
            raise AssertionError("QuoteError bekleniyordu")
    assert payload["error_type"] == "slicer_exit_error"
    assert payload["exit_code"] == 7
    assert payload["prusa_stdout"] == "stdout örneği"
    assert payload["prusa_stderr"] == "mesh error"


def test_print_area_error_is_classified() -> None:
    completed = subprocess.CompletedProcess(["prusa-slicer"], 1, stdout="", stderr="Object is outside the print area")
    with tempfile.TemporaryDirectory() as directory, patch.object(main, "slicer_command", return_value=["prusa-slicer"]), patch.object(main.subprocess, "run", return_value=completed):
        try:
            main.slice_path(Path(directory) / "model.stl", Path(directory), quote_errors=True)
        except main.QuoteError as exc:
            payload = main.error_payload(exc)
        else:
            raise AssertionError("QuoteError bekleniyordu")
    assert payload["error_type"] == "model_outside_print_area"


def test_timeout_contains_process_output() -> None:
    timeout = subprocess.TimeoutExpired(["prusa-slicer"], 240, output=b"partial out", stderr=b"partial err")
    with tempfile.TemporaryDirectory() as directory, patch.object(main, "slicer_command", return_value=["prusa-slicer"]), patch.object(main.subprocess, "run", side_effect=timeout):
        try:
            main.slice_path(Path(directory) / "model.stl", Path(directory), quote_errors=True)
        except main.QuoteError as exc:
            payload = main.error_payload(exc)
        else:
            raise AssertionError("QuoteError bekleniyordu")
    assert payload["error_type"] == "slicer_timeout"
    assert payload["prusa_stdout"] == "partial out"
    assert payload["prusa_stderr"] == "partial err"


def test_model_transforms_are_sent_to_prusaslicer() -> None:
    def successful_run(args, **kwargs):
        output = Path(args[args.index("--output") + 1])
        output.write_text("; filament used [g] = 10\n", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    transforms = {"scale_percent": 125, "rotate_x": 90, "rotate_y": 180, "rotate_z": 270}
    with tempfile.TemporaryDirectory() as directory, patch.object(main, "slicer_command", return_value=["prusa-slicer"]), patch.object(main.subprocess, "run", side_effect=successful_run) as run:
        main.slice_path(Path(directory) / "model.stl", Path(directory), quote_errors=True, transforms=transforms)
    args = run.call_args.args[0]
    assert args[args.index("--scale") + 1] == "125%"
    assert args[args.index("--rotate-x") + 1] == "90"
    assert args[args.index("--rotate-y") + 1] == "180"
    assert args[args.index("--rotate") + 1] == "270"


def test_scale_validation() -> None:
    try:
        main.quote_transforms({"scale_percent": 500})
    except main.QuoteError as exc:
        assert exc.error_type == "request_validation_error"
    else:
        raise AssertionError("QuoteError bekleniyordu")


def test_quantity_multiplies_total_print_time() -> None:
    priced = main.priced_result({"filament_used_g": 2, "estimated_print_time": "28m 36s"}, {"quantity": 20})
    assert priced["quantity"] == 20
    assert priced["unit_print_time_text"] == "28m 36s"
    assert priced["print_time_text"] == "9h 32m"
