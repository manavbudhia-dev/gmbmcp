import asyncio, importlib, os, sys, threading, time
from unittest import mock
import pytest

SECRET = "a" * 32
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["GBP_MCP_HOME"] = "/tmp/gbp-test-home"

@pytest.fixture()
def H():
    sys.argv = ["server.py", "--http"]
    os.environ["GBP_SECRET"] = SECRET
    import server
    importlib.reload(server)
    from starlette.testclient import TestClient
    return server, TestClient(server.mcp.streamable_http_app())

def test_routes_and_secret(H):
    S, c = H
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get(f"/{SECRET}/dashboard").status_code == 200
    assert c.get("/wrong/dashboard").status_code == 404
    assert c.get("/wrong/api/locations").status_code == 404
    assert c.get(f"/{SECRET}/dashboard").headers["referrer-policy"] == "no-referrer"

def test_bad_params_400(H):
    S, c = H
    assert c.get(f"/{SECRET}/api/data?account=x&location=1").status_code == 400
    assert c.get(f"/{SECRET}/api/data?account=1&location=1/../../x").status_code == 400
    assert c.get(f"/{SECRET}/api/data?account=1&location=1&days=abc").status_code == 400

def test_locations_paginated_cached_singleflight(H):
    S, c = H
    calls = []
    def fake(method, url, params=None, body=None):
        calls.append((url, dict(params or {})))
        time.sleep(0.05)
        if url.endswith("/accounts"):
            return {"accounts": [{"name": "accounts/1"}]}
        if (params or {}).get("pageToken") == "T2":
            return {"locations": [{"name": "locations/3", "title": "C"}]}
        return {"locations": [{"name": "locations/1", "title": "A"}, {"name": "locations/2"}],
                "nextPageToken": "T2"}
    with mock.patch.object(S, "_call", fake):
        out = []
        ts = [threading.Thread(target=lambda: out.append(c.get(f"/{SECRET}/api/locations").json()))
              for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
    assert all(len(o) == 3 for o in out), out[:1]
    assert len(calls) == 3, f"expected 1 accounts + 2 location pages total, got {len(calls)}"  # single-flight + cache

def test_locations_serves_stale_on_failure(H):
    S, c = H
    good = lambda m, u, params=None, body=None: ({"accounts": [{"name": "accounts/1"}]} if u.endswith("/accounts")
                                                 else {"locations": [{"name": "locations/1", "title": "A"}]})
    with mock.patch.object(S, "_call", good):
        assert len(c.get(f"/{SECRET}/api/locations").json()) == 1
    # expire cache then make Google fail (e.g. 429)
    import time as _t
    real = _t.time
    with mock.patch.object(S.time, "time", lambda: real() + 1000), \
         mock.patch.object(S, "_call", side_effect=S.GoogleAPIError(429, "quota")):
        r = c.get(f"/{SECRET}/api/locations")
        assert r.status_code == 200 and len(r.json()) == 1

def test_tools_registered_with_schema(H):
    S, c = H
    tools = asyncio.run(S.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {"list_accounts", "list_locations", "get_location", "search_categories", "update_location",
                     "set_description", "list_reviews", "reply_to_review", "create_post", "get_performance"}
    cp = next(t for t in tools if t.name == "create_post")
    assert "dry_run" in cp.inputSchema["properties"] and "summary" in cp.inputSchema["required"]
    assert cp.inputSchema["properties"]["dry_run"]["default"] is True

def test_performance_parse(H):
    S, c = H
    payload = {"multiDailyMetricTimeSeries": [{"dailyMetricTimeSeries": [
        {"dailyMetric": "CALL_CLICKS", "timeSeries": {"datedValues": [
            {"date": {"year": 2026, "month": 10, "day": 1}, "value": "4"},
            {"date": {"year": 2026, "month": 10, "day": 2}}]}}]}]}
    with mock.patch.object(S, "_call", return_value=payload):
        out = S._performance("1", 30)
    assert out["CALL_CLICKS"] == [{"date": "2026-10-01", "value": 4}, {"date": "2026-10-02", "value": 0}]

def test_data_endpoint_partial_failure(H):
    S, c = H
    def fake(m, u, params=None, body=None):
        if "reviews" in u: raise RuntimeError("v4 quota")
        if "fetchMulti" in u: return {}
        return {"title": "Shop", "profile": {"description": "x" * 300}}
    with mock.patch.object(S, "_call", fake):
        j = c.get(f"/{SECRET}/api/data?account=1&location=2").json()
    assert j["reviews"] is None and "reviews" in j["errors"] and j["completeness"]["score"] > 0
