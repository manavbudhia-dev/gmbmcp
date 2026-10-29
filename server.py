"""Google Business Profile (GMB) MCP server.

Run once to authorise:      python server.py --auth
Local (Claude Desktop/Code): python server.py            (stdio)
Hosted (claude.ai website):  python server.py --http     (needs GBP_SECRET and
                             GBP_TOKEN_JSON env vars; see README)

All write tools default to dry_run=True, so nothing changes on your live
profile until you (or Claude, after asking you) explicitly pass dry_run=False.
"""
import asyncio
import functools
import hashlib
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from mcp.server.fastmcp import FastMCP
from starlette.responses import HTMLResponse, JSONResponse

SCOPES = ["https://www.googleapis.com/auth/business.manage"]
HOME = Path(os.environ.get("GBP_MCP_HOME", Path.home() / ".gbp-mcp"))
CLIENT_SECRET = Path(os.environ.get("GBP_CLIENT_SECRET", HOME / "client_secret.json"))
TOKEN_FILE = HOME / "token.json"

ACCOUNTS_API = "https://mybusinessaccountmanagement.googleapis.com/v1"
INFO_API = "https://mybusinessbusinessinformation.googleapis.com/v1"
V4_API = "https://mybusiness.googleapis.com/v4"  # reviews + posts still live here

DEFAULT_READ_MASK = (
    "name,title,phoneNumbers,categories,storefrontAddress,websiteUri,"
    "regularHours,specialHours,profile,openInfo,serviceArea,metadata"
)
EDITABLE_FIELDS = {
    "title", "phoneNumbers", "categories", "websiteUri", "regularHours",
    "specialHours", "profile", "openInfo", "serviceArea", "storefrontAddress",
    "serviceItems",
}

HTTP_MODE = "--http" in sys.argv
SECRET = os.environ.get("GBP_SECRET", "")
if HTTP_MODE and not re.fullmatch(r"[A-Za-z0-9_-]{24,}", SECRET):
    sys.exit("GBP_SECRET must be 24+ chars of letters, digits, - or _ (use secrets.token_urlsafe(32)).")

# claude.ai custom connectors call the server from Anthropic's cloud over HTTPS.
# The secret in the URL path is what keeps strangers out, so treat it like a password.
mcp = FastMCP(
    "google-business-profile",
    host="0.0.0.0" if HTTP_MODE else "127.0.0.1",
    port=int(os.environ.get("PORT", "8000")),
    streamable_http_path=f"/{SECRET}/mcp" if SECRET else "/mcp",
    stateless_http=True,
    log_level="WARNING",  # INFO access logs would print the secret URL path
)


# --------------------------------------------------------------------- auth
_ID = re.compile(r"[0-9]+")
_RID = re.compile(r"[A-Za-z0-9_-]{1,200}")
_cred_lock = threading.Lock()
_post_lock = threading.Lock()
_recent_posts: dict[str, float] = {}


class GoogleAPIError(RuntimeError):
    """Google answered with an HTTP error. `status` lets callers tell 'rejected' from 'maybe applied'."""
    def __init__(self, status: int, msg: str):
        super().__init__(f"Google API {status}: {msg}")
        self.status = status


class OutcomeUnknown(RuntimeError):
    """The request may have reached Google but we never saw the answer (timeout / dropped connection)."""


def _save_token(creds) -> None:
    """Atomic write with 0600 perms so a crash or race never leaves a corrupt token."""
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(creds.to_json())
    os.replace(tmp, TOKEN_FILE)


def authorise() -> None:
    """Interactive one-time OAuth flow. Opens a browser."""
    if not CLIENT_SECRET.exists():
        sys.exit(f"Missing OAuth client file: {CLIENT_SECRET}\nSee README step 3.")
    HOME.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:  # without it the hosted server dies after 1 hour
        sys.exit("Google returned no refresh token. Remove the app at "
                 "myaccount.google.com/permissions and run --auth again.")
    _save_token(creds)
    print(f"Saved token to {TOKEN_FILE}")


_cached: Credentials | None = None


def _credentials(bad_token: str | None = None) -> Credentials:
    """Thread-safe: one lock covers load + refresh, so concurrent requests never
    refresh twice or half-write token.json. After a 401 pass bad_token to force
    a refresh (skipped if another thread already refreshed)."""
    global _cached
    with _cred_lock:
        env_token = os.environ.get("GBP_TOKEN_JSON")
        if _cached is None:
            try:
                if env_token:
                    _cached = Credentials.from_authorized_user_info(json.loads(env_token), SCOPES)
                elif TOKEN_FILE.exists():
                    _cached = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
                else:
                    raise RuntimeError("Not authorised yet. Run: python server.py --auth")
            except (ValueError, KeyError) as e:  # bad JSON / missing fields in the saved login
                raise RuntimeError("Saved Google login is malformed. Re-run: python server.py --auth "
                                   "(or fix GBP_TOKEN_JSON).") from e
        if bad_token and _cached.token == bad_token:
            _cached.token = None
        if not _cached.valid:
            if not _cached.refresh_token:
                raise RuntimeError("No refresh token. Re-run: python server.py --auth")
            try:
                _cached.refresh(Request())
            except RefreshError as e:
                raise RuntimeError("Google rejected the saved login (revoked, or expired after 7 days "
                                   "because the OAuth app is in 'Testing'). Re-run: python server.py --auth") from e
            if not env_token:
                _save_token(_cached)
        return _cached


RETRY_STATUS = {429, 500, 502, 503, 504}


def _retry_after(r) -> float | None:
    try:
        return min(float(r.headers.get("Retry-After", "")), 10.0)
    except (TypeError, ValueError):
        return None


def _call(method: str, url: str, params: dict | None = None, body: dict | None = None):
    # Retry only idempotent verbs; a retried POST could publish twice.
    tries = 4 if method in ("GET", "PUT", "PATCH") else 1
    i, refreshed = 0, False
    while True:
        token = _credentials().token
        try:
            r = requests.request(method, url, params=params, json=body, timeout=30,
                                 headers={"Authorization": f"Bearer {token}"})
        except requests.RequestException as e:
            if i + 1 < tries:
                i += 1
                time.sleep(0.5 * 2 ** i)
                continue
            msg = f"Network error talking to Google ({e.__class__.__name__})"
            if method == "POST" and not isinstance(e, requests.ConnectTimeout):
                # request may have been delivered; caller must not assume it failed
                raise OutcomeUnknown(msg + ". The request may or may not have been applied; "
                                     "check your profile before retrying.") from e
            raise RuntimeError(msg) from e
        if r.status_code == 401 and not refreshed:  # token revoked/expired early: safe to retry any verb
            refreshed = True
            _credentials(bad_token=token)
            continue
        if r.status_code in RETRY_STATUS and i + 1 < tries:
            i += 1
            time.sleep(_retry_after(r) or 2 ** i)
            continue
        break
    if not r.ok:
        try:
            err = r.json().get("error")
            msg = err.get("message", r.text) if isinstance(err, dict) else (err or r.text)
        except (ValueError, AttributeError):  # non-JSON body, or JSON that isn't an object
            msg = r.text
        raise GoogleAPIError(r.status_code, str(msg)[:500])
    try:
        return r.json() if r.text else {}
    except ValueError:
        if method == "POST":  # Google answered 2xx: the write most likely happened
            raise OutcomeUnknown("Google accepted the request but returned an unreadable response; "
                                 "check your profile before retrying.") from None
        raise RuntimeError("Google returned a non-JSON response") from None


def _paginate(url: str, params: dict, key: str, max_pages: int = 20) -> list:
    """Follow nextPageToken so lists aren't silently truncated at the first page."""
    out, token = [], None
    for _ in range(max_pages):
        data = _call("GET", url, params={**params, **({"pageToken": token} if token else {})})
        out += data.get(key, [])
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def _loc(location: str) -> str:
    """'123' or 'locations/123' -> 'locations/123'. Anything else is rejected so a
    crafted id (e.g. '1/../../x') can't redirect the request to another API path."""
    n = str(location).strip().removeprefix("locations/")
    if not _ID.fullmatch(n):
        raise ValueError(f"Invalid location id: {location!r}")
    return f"locations/{n}"


def _acct(account_id: str) -> str:
    n = str(account_id).strip().removeprefix("accounts/")
    if not _ID.fullmatch(n):
        raise ValueError(f"Invalid account id: {account_id!r}")
    return f"accounts/{n}"


def _rid(review_id: str) -> str:
    if not _RID.fullmatch(str(review_id)):
        raise ValueError(f"Invalid review id: {review_id!r}")
    return review_id


# ------------------------------------------------------------------ reading
def list_accounts() -> dict:
    """List the Business Profile accounts the signed-in Google user can manage."""
    return _call("GET", f"{ACCOUNTS_API}/accounts")


def list_locations(account_id: str, page_size: int = 50) -> dict:
    """List locations (business listings) under an account (e.g. 'accounts/123')."""
    return _call(
        "GET",
        f"{INFO_API}/{_acct(account_id)}/locations",
        params={"readMask": "name,title,storefrontAddress,websiteUri,metadata", "pageSize": page_size},
    )


def get_location(location: str, read_mask: str = DEFAULT_READ_MASK) -> dict:
    """Get full details of one listing (name, categories, hours, description, etc.)."""
    return _call("GET", f"{INFO_API}/{_loc(location)}", params={"readMask": read_mask})


def search_categories(search_term: str, region_code: str = "IN", language_code: str = "en") -> dict:
    """Search valid Google business categories (use the returned 'name' values)."""
    return _call(
        "GET",
        f"{INFO_API}/categories",
        params={"regionCode": region_code, "languageCode": language_code,
                "searchTerm": search_term, "view": "BASIC", "pageSize": 20},
    )


# ------------------------------------------------------------------ editing
def update_location(location: str, fields: dict, dry_run: bool = True) -> dict:
    """Update listing fields. `fields` uses Business Information API names, e.g.
    {"websiteUri": "https://x.com"} or {"profile": {"description": "..."}}.
    Allowed top-level keys: title, phoneNumbers, categories, websiteUri,
    regularHours, specialHours, profile, openInfo, serviceArea,
    storefrontAddress, serviceItems. dry_run=True only previews the change."""
    if not isinstance(fields, dict) or not fields:
        raise ValueError("`fields` must be a non-empty object, e.g. {\"websiteUri\": \"https://x.com\"}")
    bad = set(fields) - EDITABLE_FIELDS
    if bad:
        raise ValueError(f"Unsupported field(s): {sorted(bad)}. Allowed: {sorted(EDITABLE_FIELDS)}")
    mask = ",".join(sorted(fields))
    if dry_run:
        current = _call("GET", f"{INFO_API}/{_loc(location)}", params={"readMask": mask})
        current.pop("name", None)
        return {"dry_run": True, "updateMask": mask, "current": current, "proposed": fields,
                "note": "Nothing was changed. Call again with dry_run=False to apply."}
    return _call("PATCH", f"{INFO_API}/{_loc(location)}", params={"updateMask": mask}, body=fields)


def set_description(location: str, description: str, dry_run: bool = True) -> dict:
    """Set the business description (max 750 characters)."""
    if len(description) > 750:
        raise ValueError(f"Description is {len(description)} chars; Google allows 750.")
    return update_location(location, {"profile": {"description": description}}, dry_run)


# ------------------------------------------------------------------ reviews
def list_reviews(account_id: str, location_id: str, page_size: int = 20,
                 order_by: str = "updateTime desc") -> dict:
    """List customer reviews for a location."""
    return _call(
        "GET",
        f"{V4_API}/{_acct(account_id)}/{_loc(location_id)}/reviews",
        params={"pageSize": page_size, "orderBy": order_by},
    )


def reply_to_review(account_id: str, location_id: str, review_id: str,
                    comment: str, dry_run: bool = True) -> dict:
    """Post (or overwrite) the owner reply on a review (max 4096 characters)."""
    path = f"{V4_API}/{_acct(account_id)}/{_loc(location_id)}/reviews/{_rid(review_id)}/reply"
    if not comment or not comment.strip():
        raise ValueError("Reply is empty.")
    if len(comment) > 4096:
        raise ValueError(f"Reply is {len(comment)} chars; limit is 4096.")
    if dry_run:
        return {"dry_run": True, "review_id": review_id, "proposed_reply": comment}
    return _call("PUT", path, body={"comment": comment})


# -------------------------------------------------------------------- posts
CTA_TYPES = {"BOOK", "ORDER", "SHOP", "LEARN_MORE", "SIGN_UP", "CALL"}


def create_post(account_id: str, location_id: str, summary: str,
                call_to_action: str | None = None, url: str | None = None,
                language_code: str = "en", dry_run: bool = True) -> dict:
    """Publish a 'What's new' post (max 1500 chars). call_to_action is one of
    BOOK, ORDER, SHOP, LEARN_MORE, SIGN_UP, CALL; url required except for CALL."""
    if not summary or not summary.strip():
        raise ValueError("Post text is empty.")
    if len(summary) > 1500:
        raise ValueError(f"Post is {len(summary)} chars; limit is 1500.")
    if call_to_action and call_to_action not in CTA_TYPES:
        raise ValueError(f"call_to_action must be one of {sorted(CTA_TYPES)}")
    if url and not call_to_action:
        raise ValueError("`url` was given without `call_to_action`; it would be silently dropped.")
    if call_to_action == "CALL" and url:
        raise ValueError("CALL posts use the listing's phone number; do not pass a url.")
    if call_to_action and call_to_action != "CALL" and not (url and url.startswith(("https://", "http://"))):
        raise ValueError(f"call_to_action={call_to_action} needs an http(s) url.")
    path = f"{V4_API}/{_acct(account_id)}/{_loc(location_id)}/localPosts"  # validates ids in dry-run too
    body: dict = {"languageCode": language_code, "summary": summary, "topicType": "STANDARD"}
    if call_to_action:
        body["callToAction"] = {"actionType": call_to_action}
        if url:
            body["callToAction"]["url"] = url
    if dry_run:
        return {"dry_run": True, "proposed_post": body}
    # The key covers everything that makes a post distinct, not just its text.
    key = hashlib.sha256(json.dumps([path, body], sort_keys=True).encode()).hexdigest()
    with _post_lock:  # blocks double-clicks / client retries from publishing twice
        now = time.time()
        for k in [k for k, t in _recent_posts.items() if now - t > 600]:
            del _recent_posts[k]
        if key in _recent_posts:
            raise RuntimeError("An identical post was submitted in the last 10 minutes; not posting a duplicate. "
                               "If an earlier attempt reported an unknown outcome, check your profile first.")
        _recent_posts[key] = now
    try:
        return _call("POST", path, body=body)
    except Exception as e:
        # Release the guard only when we KNOW nothing was published (Google rejected it, or we
        # never reached Google). After a timeout / dropped connection / 5xx the post may exist,
        # so keep the guard: a blind retry would publish a duplicate.
        definitely_not_posted = (isinstance(e, GoogleAPIError) and e.status < 500) or \
            (type(e) is RuntimeError)
        if definitely_not_posted:
            with _post_lock:
                _recent_posts.pop(key, None)
        raise


# ------------------------------------------------- performance + dashboard
PERF_API = "https://businessprofileperformance.googleapis.com/v1"
PERF_METRICS = [
    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS", "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
    "BUSINESS_IMPRESSIONS_MOBILE_MAPS", "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
    "CALL_CLICKS", "WEBSITE_CLICKS", "BUSINESS_DIRECTION_REQUESTS",
]
STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


def _performance(location: str, days: int = 30) -> dict:
    days = min(max(int(days), 1), 540)
    end = datetime.now(timezone.utc).date() - timedelta(days=2)  # Google's data lags ~2 days
    start = end - timedelta(days=days - 1)
    params = {"dailyMetrics": PERF_METRICS}
    for key, d in (("startDate", start), ("endDate", end)):
        params.update({f"dailyRange.{key}.year": d.year, f"dailyRange.{key}.month": d.month,
                       f"dailyRange.{key}.day": d.day})
    data = _call("GET", f"{PERF_API}/{_loc(location)}:fetchMultiDailyMetricsTimeSeries", params=params)
    out: dict = {}
    for group in data.get("multiDailyMetricTimeSeries", []):
        for m in group.get("dailyMetricTimeSeries", []):
            pts = m.get("timeSeries", {}).get("datedValues", [])
            out[m["dailyMetric"]] = [
                {"date": "{year:04d}-{month:02d}-{day:02d}".format(**p["date"]),
                 "value": int(p.get("value", 0))} for p in pts]
    return out


def _reviews_summary(account: str, location: str) -> dict:
    data = _call("GET", f"{V4_API}/{_acct(account)}/{_loc(location)}/reviews",
                 params={"pageSize": 50, "orderBy": "updateTime desc"})
    revs = data.get("reviews", [])
    dist = {i: 0 for i in range(1, 6)}
    rows = []
    for r in revs:
        n = STARS.get(r.get("starRating"), 0)
        if n:
            dist[n] += 1
        rows.append({"reviewer": (r.get("reviewer") or {}).get("displayName") or "Anonymous",
                     "stars": n, "comment": (r.get("comment") or "")[:300],
                     "date": r.get("updateTime", "")[:10], "replied": "reviewReply" in r,
                     "id": r.get("reviewId")})
    return {"average": data.get("averageRating"), "total": data.get("totalReviewCount", len(revs)),
            "distribution": dist, "unreplied": sum(not x["replied"] for x in rows),
            "recent": rows[:10], "sample_size": len(rows)}


def _completeness(loc: dict) -> dict:
    cats = loc.get("categories") or {}
    desc = (loc.get("profile") or {}).get("description", "")
    checks = [
        ("Description is 250+ characters", len(desc) >= 250,
         "Write 250-750 characters: services, area served, what makes you different."),
        ("Primary category set", bool(cats.get("primaryCategory")),
         "Pick the single category that best describes your main service."),
        ("Extra categories added", bool(cats.get("additionalCategories")),
         "Add 2-5 secondary categories that genuinely apply."),
        ("Website linked", bool(loc.get("websiteUri")), "Add your website URL."),
        ("Phone number set", bool((loc.get("phoneNumbers") or {}).get("primaryPhone")),
         "Add a phone number customers can reach."),
        ("Opening hours set", bool((loc.get("regularHours") or {}).get("periods")),
         "Add regular weekly opening hours."),
        ("Holiday / special hours set", bool((loc.get("specialHours") or {}).get("specialHourPeriods")),
         "Add upcoming holiday or special hours."),
        ("Address or service area set", bool(loc.get("storefrontAddress") or loc.get("serviceArea")),
         "Add a storefront address or the areas you serve."),
        ("Listing marked open", (loc.get("openInfo") or {}).get("status") == "OPEN",
         "Mark the business as open if it is trading."),
    ]
    items = [{"label": a, "ok": ok, "tip": tip} for a, ok, tip in checks]
    return {"score": round(100 * sum(i["ok"] for i in items) / len(items)), "checks": items}


def get_performance(location: str, days: int = 30) -> dict:
    """Daily search/maps impressions, call clicks, website clicks and direction
    requests for the last `days` days (data lags about 2 days)."""
    return _performance(location, days)


async def _safe(fn):
    nc = {"Cache-Control": "no-store"}
    try:
        return JSONResponse(await asyncio.to_thread(fn), headers=nc)
    except ValueError as e:  # bad ids / params from the browser
        return JSONResponse({"error": str(e)}, status_code=400, headers=nc)
    except Exception as e:  # surface Google errors to the dashboard banner
        return JSONResponse({"error": str(e)}, status_code=502, headers=nc)


if HTTP_MODE:
    _DASH = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")  # fail fast if missing
    _HDRS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
             "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}

    @mcp.custom_route(f"/{SECRET}/dashboard", methods=["GET"])
    async def dashboard(request):
        return HTMLResponse(_DASH, headers=_HDRS)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request):
        return JSONResponse({"ok": True})

    _loc_cache: dict = {"at": 0.0, "rows": None}
    _loc_lock = threading.Lock()  # single-flight: concurrent page loads share one fetch

    def _all_locations() -> list:
        with _loc_lock:
            now = time.time()
            if _loc_cache["rows"] is not None and now - _loc_cache["at"] < 300:
                return _loc_cache["rows"]
            try:
                rows = []
                for a in _paginate(f"{ACCOUNTS_API}/accounts", {"pageSize": 20}, "accounts"):
                    for l in _paginate(f"{INFO_API}/{_acct(a['name'])}/locations",
                                       {"readMask": "name,title", "pageSize": 100}, "locations"):
                        rows.append({"account": a["name"], "location": l["name"],
                                     "title": l.get("title", l["name"])})
            except Exception:
                if _loc_cache["rows"] is not None:  # quota/outage: serve the last good list
                    return _loc_cache["rows"]
                raise
            _loc_cache.update(at=now, rows=rows)
            return rows

    @mcp.custom_route(f"/{SECRET}/api/locations", methods=["GET"])
    async def api_locations(request):
        return await _safe(_all_locations)

    @mcp.custom_route(f"/{SECRET}/api/data", methods=["GET"])
    async def api_data(request):
        q = request.query_params
        account, location = q.get("account", ""), q.get("location", "")
        days = q.get("days", "30")

        def work():
            _acct(account), _loc(location)  # validate first -> 400 on junk
            days_n = int(days)
            loc = _call("GET", f"{INFO_API}/{_loc(location)}", params={"readMask": DEFAULT_READ_MASK})
            out = {"title": loc.get("title"), "completeness": _completeness(loc), "errors": {}}
            for key, fn in (("reviews", lambda: _reviews_summary(account, location)),
                            ("performance", lambda: _performance(location, days_n))):
                try:
                    out[key] = fn()
                except Exception as e:  # one failing API shouldn't blank the page
                    out[key], out["errors"][key] = None, str(e)
            return out
        return await _safe(work)


def _threaded(fn):
    """Run blocking Google calls in a worker thread so one slow request can't
    freeze the event loop (and the dashboard) for everyone else."""
    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)
    return wrapper


for _fn in (list_accounts, list_locations, get_location, search_categories, update_location,
            set_description, list_reviews, reply_to_review, create_post, get_performance):
    mcp.tool()(_threaded(_fn))


if __name__ == "__main__":
    if "--auth" in sys.argv:
        authorise()
    elif HTTP_MODE:
        try:
            _credentials()  # fail fast on missing/bad login instead of at first request
        except Exception as e:
            sys.exit(f"Startup check failed: {e}")
        print(f"Serving on port {mcp.settings.port} at /<GBP_SECRET>/mcp", file=sys.stderr)
        mcp.run(transport="streamable-http")
    else:
        mcp.run()  # stdio
