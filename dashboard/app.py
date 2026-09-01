"""Local server for the Job Market Intelligence Dashboard"""
from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import socket
from socketserver import BaseServer
from typing import Any, cast
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError

from dashboard.tracking import ApplicationTrackingRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_MART_PATH = PROJECT_ROOT / "data" / "gold" / "current_opportunities" / "mart.json"
DEFAULT_TRACKING_PATH = PROJECT_ROOT / "data" / "application_tracking" / "state.json"
MAX_REQUEST_BYTES = 64 * 1024

"""load_opportunities_mart(path): Load and minimally validate the current-opportunities Gold mart"""
def load_opportunities_mart(path: Path = DEFAULT_MART_PATH) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Current-opportunities mart not found: {path}. Run scripts/build_gold.py first."
        )

    payload = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(payload, dict) or not isinstance(payload.get("opportunities"), list):
        raise ValueError("Current-opportunities mart must contain an opportunities list")

    return cast(dict[str, Any], payload)

"""DashboardRequestHandler(SimpleHTTPRequestHandler): Serve dashboard assets and live opportunities endpoint"""
class DashboardRequestHandler(SimpleHTTPRequestHandler):

    mart_path = DEFAULT_MART_PATH
    static_dir = STATIC_DIR
    tracking_repository = ApplicationTrackingRepository(DEFAULT_TRACKING_PATH)

    """__init__(self, request, client_address, server): Serve static files from the dashboard asset directory"""
    def __init__(
        self,
        request: socket | tuple[bytes, socket],
        client_address: tuple[str, int],
        server: BaseServer,
    ) -> None:
        super().__init__(
            request,
            client_address,
            server,
            directory=str(self.static_dir),
        )

    """do_GET(self): Route API requests and delegate asset requests to the static server"""
    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/opportunities":
            self._send_json(load_opportunities_mart(self.mart_path))
            return
        if path == "/api/application-tracking":
            self._send_json(self.tracking_repository.load().model_dump(mode="json"))
            return
        if path == "/api/health":
            try:
                mart = load_opportunities_mart(self.mart_path)
                self._send_json(
                    {
                        "status": "ok",
                        "row_count": mart.get("row_count", 0),
                        "generated_at": mart.get("generated_at"),
                    }
                )
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                self._send_json(
                    {"status": "error", "error": str(exc)},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
            return
        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    """do_PUT(self): Validate and persist one candidate-controlled tracking record."""
    def do_PUT(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        prefix = "/api/application-tracking/"

        if not path.startswith(prefix):
            self._send_json({"error": "Endpoint not found"}, status=HTTPStatus.NOT_FOUND)
            return

        job_id = unquote(path.removeprefix(prefix)).strip()
        if not job_id or "/" in job_id:
            self._send_json({"error": "A valid job ID is required"}, status=HTTPStatus.BAD_REQUEST)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                raise ValueError("Request body must be between 1 byte and 64 KB")

            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")

            record = self.tracking_repository.update(job_id, payload)
            self._send_json(record.model_dump(mode="json"))

        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    """_send_json(self, object payload, HttpStatus status): Writes one UTF-8 JSON response"""
    def _send_json(
        self,
        payload: object,
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    """log_message(self, format, *args): Keeps routine local asset requests out of application logs."""
    def log_message(self, format: str, *args: object) -> None:
        return

"""create_server(str host, int port, path mart_path, path static_dir, path tracking_path): Create a configured threaded dashboard server."""
def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    mart_path: Path = DEFAULT_MART_PATH,
    static_dir: Path = STATIC_DIR,
    tracking_path: Path = DEFAULT_TRACKING_PATH,
) -> ThreadingHTTPServer:
    handler = type(
        "ConfiguredDashboardRequestHandler",
        (DashboardRequestHandler,),
        {
            "mart_path": mart_path,
            "static_dir": static_dir,
            "tracking_repository": ApplicationTrackingRepository(tracking_path),
        },
    )
    return ThreadingHTTPServer((host, port), handler)

"""build_parser(): Build command-line arguments for the local dashboard server."""
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the JMIP opportunities dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mart", type=Path, default=DEFAULT_MART_PATH)
    parser.add_argument("--tracking", type=Path, default=DEFAULT_TRACKING_PATH)
    return parser

"""main(list[str] args, ): Run the dashboard until interrupted."""
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = create_server(
        args.host,
        args.port,
        mart_path=args.mart,
        tracking_path=args.tracking,
    )
    print(f"JMIP dashboard available at http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
