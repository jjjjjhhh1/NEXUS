"""Single-process HTTP rate limits and trusted-proxy address resolution."""
import hashlib
import time
from fastapi import Request

RATE_LIMITS: dict[str, tuple[int, float]] = {
    # path prefix -> (max calls, window seconds)
    "/api/messages": (40, 60.0),
    "/api/actions": (30, 60.0),
    "/api/ratings": (60, 60.0),
}


RATE_BYPASS_PATHS = {"/api/health", "/api/session", "/api/capabilities", "/api/overview"}


_rate_buckets: dict[str, list[float]] = {}


def _rate_key(request: Request) -> str:
    """Whose budget is this request spending?

    Three keys were tried, in this order, and the failures are the reason:

    * **Session token** — protected nothing. The old flow handed out a fresh
      session to anyone who asked, so a scripted caller could mint one per
      request and never touch a limit, while the budget that existed throttled
      one impatient human. A budget on something the caller can renew for free
      is not a budget.
    * **Client address** — works, but punishes everyone behind one NAT. A
      review panel on a conference wifi, or a university office, is dozens of
      people on a single address; the first one to type quickly spends the
      budget for all of them.
    * **Operator account** — the thing we actually want to cap. The LLM key is
      a shared resource, so "how much of it may this login spend" is the
      question worth asking, and it is the one an operator can reason about.
      Falls back to the address for requests with no session, which is where
      the scanning and the credential stuffing are.
    """
    who = request.cookies.get("nexus_session") or request.cookies.get("nexus_demo")
    if who:
        return f"session:{hashlib.sha256(who.encode()).hexdigest()[:16]}"
    return f"addr:{client_address(request)}"


def _rate_exceeded(request: Request) -> bool:
    now = time.monotonic()
    for path, (limit, window) in RATE_LIMITS.items():
        if not request.url.path.startswith(path):
            continue
        key = f"{_rate_key(request)}|{path}"
        hits = [t for t in _rate_buckets.get(key, []) if now - t < window]
        if len(hits) >= limit:
            _rate_buckets[key] = hits
            return True
        hits.append(now)
        _rate_buckets[key] = hits
        break
    # Drop buckets nobody has used recently, so the map cannot grow forever.
    if len(_rate_buckets) > 512:
        stale = [k for k, v in _rate_buckets.items() if not v or now - v[-1] > 300]
        for k in stale:
            _rate_buckets.pop(k, None)
    return False


def client_address(request: Request) -> str:
    """The address to rate-limit and to record.

    ``request.client.host`` is the real peer only when nothing is proxying. A
    public deployment puts nginx in front, and then the peer is always 127.0.0.1
    — every visitor would share one rate-limit bucket, and one person's typo
    would lock out everyone. The X-Forwarded-For hop is only honoured because
    the proxy is configured to overwrite it (see deploy/nginx.conf); if it were
    passed through from the client, this would be a trivially forged identity.
    """
    if request.client and request.client.host not in {"127.0.0.1", "::1"}:
        return request.client.host
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "-"


