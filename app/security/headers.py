"""Middleware ASGI: header di sicurezza e redirect HTTPS obbligatorio in produzione."""
from starlette.datastructures import URL, MutableHeaders
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

# I probe degli host (App Service, Fly, Render) chiamano /health in HTTP interno
HTTPS_EXEMPT = {"/health"}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool):
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h.setdefault("Content-Security-Policy", CSP)
                h.setdefault("X-Frame-Options", "DENY")
                h.setdefault("X-Content-Type-Options", "nosniff")
                h.setdefault("Referrer-Policy", "no-referrer")
                h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
                if not scope["path"].startswith("/static/"):
                    h.setdefault("Cache-Control", "no-store")
                if self.hsts:
                    h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            await send(message)

        await self.app(scope, receive, send_wrapper)


class HTTPSRedirectMiddleware:
    """Come quello di Starlette, ma rispetta lo schema impostato da --proxy-headers ed esenta /health."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["scheme"] != "https" and scope["path"] not in HTTPS_EXEMPT:
            url = URL(scope=scope).replace(scheme="https", port=None)
            await RedirectResponse(str(url), status_code=308)(scope, receive, send)
            return
        await self.app(scope, receive, send)
