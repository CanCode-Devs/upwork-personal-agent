from __future__ import annotations

import asyncio
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
from pydantic import AnyUrl, BaseModel

from app.config import Settings, get_settings
from app.upwork.token_store import FileTokenStorage, token_storage

OAuthMode = Literal["cli", "web"]
WEB_CALLBACK_PATH = "/upwork/callback"


class OAuthCallbackPayload(BaseModel):
    code: str | None = None
    state: str | None = None
    error: str | None = None


class WebOAuthFlow:
    def __init__(self) -> None:
        self.redirect_url: str | None = None
        self.redirect_ready = asyncio.Event()
        self.payload = OAuthCallbackPayload()
        self.code_ready = asyncio.Event()
        self.finished = asyncio.Event()
        self.exception: BaseException | None = None
        self.task: asyncio.Task[None] | None = None
        self.started_at = time.monotonic()


class _CallbackState:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.code: str | None = None
        self.state: str | None = None
        self.error: str | None = None


_web_flow: WebOAuthFlow | None = None
_last_oauth_error: str = ""


def set_last_oauth_error(message: str) -> None:
    global _last_oauth_error
    _last_oauth_error = message[:400]


def pop_last_oauth_error() -> str:
    global _last_oauth_error
    message = _last_oauth_error
    _last_oauth_error = ""
    return message


def get_web_oauth_flow() -> WebOAuthFlow | None:
    return _web_flow


def start_web_oauth_flow() -> WebOAuthFlow:
    global _web_flow
    _web_flow = WebOAuthFlow()
    return _web_flow


def clear_web_oauth_flow() -> None:
    global _web_flow
    _web_flow = None


def web_redirect_uri(settings: Settings) -> str:
    return f"http://{settings.oauth_redirect_host}:{settings.bind_port}{WEB_CALLBACK_PATH}"


def cli_redirect_uri(settings: Settings) -> str:
    return f"http://{settings.oauth_redirect_host}:{settings.oauth_redirect_port}/callback"


def _listen_host(settings: Settings) -> str:
    if settings.bind_host in {"0.0.0.0", "::"}:
        return "0.0.0.0"
    return settings.oauth_redirect_host


def _handler_factory(cb_state: _CallbackState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path not in {"/callback", "/"}:
                self.send_response(404)
                self.end_headers()
                return
            query = parse_qs(parsed.query)
            cb_state.code = (query.get("code") or [None])[0]
            cb_state.state = (query.get("state") or [None])[0]
            cb_state.error = (query.get("error") or [None])[0]
            body = b"Upwork login complete. You can close this tab."
            if cb_state.error:
                body = f"Login failed: {cb_state.error}".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            cb_state.event.set()

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def build_oauth_provider(
    settings: Settings | None = None,
    *,
    interactive: bool,
    storage: FileTokenStorage | None = None,
    mode: OAuthMode = "cli",
    web_flow: WebOAuthFlow | None = None,
) -> Any:
    settings = settings or get_settings()
    storage = storage or token_storage(settings)
    redirect_uri = web_redirect_uri(settings) if mode == "web" else cli_redirect_uri(settings)
    cb_state = _CallbackState()
    server_holder: dict[str, HTTPServer] = {}

    async def redirect_handler(url: str) -> None:
        if not interactive:
            raise RuntimeError(
                "Upwork MCP requires interactive OAuth. Connect from the dashboard."
            )
        if mode == "web":
            flow = web_flow or get_web_oauth_flow()
            if flow is None:
                raise RuntimeError("No Upwork login in progress")
            flow.redirect_url = url
            flow.redirect_ready.set()
            return
        handler = _handler_factory(cb_state)
        httpd = HTTPServer((_listen_host(settings), settings.oauth_redirect_port), handler)
        server_holder["server"] = httpd
        thread = threading.Thread(target=httpd.handle_request, daemon=True)
        thread.start()
        webbrowser.open(url)
        print(f"If the browser did not open, visit:\n{url}\n")

    async def callback_handler() -> AuthorizationCodeResult:
        if not interactive:
            raise RuntimeError(
                "Upwork MCP requires interactive OAuth. Connect from the dashboard."
            )
        if mode == "web":
            flow = web_flow or get_web_oauth_flow()
            if flow is None:
                raise RuntimeError("No Upwork login in progress")
            await asyncio.wait_for(flow.code_ready.wait(), 300)
            if flow.payload.error:
                raise RuntimeError(f"OAuth error: {flow.payload.error}")
            if not flow.payload.code:
                raise RuntimeError("OAuth callback missing code")
            return AuthorizationCodeResult(code=flow.payload.code, state=flow.payload.state)
        completed = await asyncio.to_thread(cb_state.event.wait, 300)
        httpd = server_holder.get("server")
        if httpd is not None:
            httpd.server_close()
        if not completed:
            raise TimeoutError("Timed out waiting for Upwork OAuth callback")
        if cb_state.error:
            raise RuntimeError(f"OAuth error: {cb_state.error}")
        if not cb_state.code:
            raise RuntimeError("OAuth callback missing code")
        return AuthorizationCodeResult(code=cb_state.code, state=cb_state.state)

    metadata = OAuthClientMetadata(
        client_name=settings.app_name,
        redirect_uris=[AnyUrl(redirect_uri)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )
    return OAuthClientProvider(
        server_url=settings.upwork_mcp_url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
