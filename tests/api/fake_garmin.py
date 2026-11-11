"""A fake Garmin Personal API served on localhost, so no test reaches Garmin."""

import asyncio
import base64
import json
from dataclasses import dataclass, field

from aiohttp import web

KEY_EXCHANGE_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
# Obviously fake: the client must never log or expose it.
FAKE_API_KEY = "fake-api-key-for-tests"  # pragma: allowlist secret
START_TIME = 1_800_000_000.0
DAY = 86_400.0


class FakeClock:
    """A settable clock, shared by the client and the fake server."""

    def __init__(self) -> None:
        self.now = START_TIME

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Reply:
    """A canned HTTP response, served before the fake's default behaviour."""

    status: int = 200
    body: str = "{}"
    headers: dict[str, str] = field(default_factory=dict)
    # Seconds to wait before answering, to order concurrent responses.
    delay: float = 0.0


def make_jwt(claims: dict[str, object]) -> str:
    def encode(part: dict[str, object]) -> str:
        raw = json.dumps(part).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(claims)}.c2ln"


@dataclass
class SeenRequest:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]


class FakeGarmin:
    """Issues JWTs for the fake key and serves JSON for any data path."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.base_url = ""
        self.token_lifetime = DAY
        self.token_replies: list[Reply] = []
        self.data_replies: dict[str, list[Reply]] = {}
        self.data_delay = 0.0
        self.issued: list[str] = []
        self.revoked: set[str] = set()
        self.requests: list[SeenRequest] = []
        self.app = web.Application()
        self.app.router.add_post(KEY_EXCHANGE_PATH, self._token)
        self.app.router.add_get("/{path:.*}", self._data)

    @property
    def exchanges(self) -> int:
        return sum(r.path == KEY_EXCHANGE_PATH for r in self.requests)

    def revoke_issued(self) -> None:
        self.revoked.update(self.issued)

    def _record(self, request: web.Request) -> None:
        self.requests.append(
            SeenRequest(
                method=request.method,
                path=request.path,
                query=dict(request.query),
                headers=dict(request.headers),
            )
        )

    async def _token(self, request: web.Request) -> web.Response:
        self._record(request)
        if self.token_replies:
            return _respond(self.token_replies.pop(0))
        if request.headers.get("Authorization") != f"ApiKey {FAKE_API_KEY}":
            return web.Response(status=401)
        jwt = make_jwt(
            {"exp": int(self.clock() + self.token_lifetime), "n": len(self.issued)}
        )
        self.issued.append(jwt)
        return web.Response(text=jwt, content_type="text/plain")

    async def _data(self, request: web.Request) -> web.Response:
        self._record(request)
        if self.data_delay:
            await asyncio.sleep(self.data_delay)
        replies = self.data_replies.get(request.path)
        if replies:
            reply = replies.pop(0)
            await asyncio.sleep(reply.delay)
            return _respond(reply)
        auth = request.headers.get("Authorization", "")
        bearer = auth.removeprefix("Bearer ")
        if not auth.startswith("Bearer ") or bearer not in self.issued:
            return web.Response(status=401)
        if bearer in self.revoked:
            return web.Response(status=401)
        return web.json_response({"path": request.path, "query": dict(request.query)})


def _respond(reply: Reply) -> web.Response:
    return web.Response(status=reply.status, text=reply.body, headers=reply.headers)
