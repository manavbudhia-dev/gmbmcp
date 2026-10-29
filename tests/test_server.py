"""Reproduction tests. Run against original and fixed server.py."""
import importlib, os, sys, threading, time
from unittest import mock
import pytest, requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["GBP_MCP_HOME"] = "/tmp/gbp-test-home"

@pytest.fixture()
def S():
    sys.argv = ["server.py"]
    import server
    importlib.reload(server)
    server._cached = None
    server._recent_posts.clear()
    return server

class FakeResp:
    def __init__(self, status=200, body=None, text=None):
        import json
        self.status_code = status; self.ok = status < 400
        self._b = body if body is not None else {}
        self.text = text if text is not None else json.dumps(self._b)
    def json(self):
        import json
        return json.loads(self.text)

# 1. error body where "error" is a plain string (OAuth-style / proxies)
def test_error_body_string(S):
    S._credentials = lambda bad_token=None: mock.Mock(token="t")
    with mock.patch.object(S.requests, "request",
                           return_value=FakeResp(403, text='{"error": "access_denied"}')):
        with pytest.raises(RuntimeError, match="Google API 403"):
            S._call("GET", "http://x")

# 2. credential refresh must happen once under concurrency
def test_single_refresh(S):
    calls = []
    class C:
        token=None; refresh_token="r"; valid=False
        def refresh(self, req):
            calls.append(1); time.sleep(0.2); self.token="new"; self.valid=True
    os.environ['GBP_TOKEN_JSON']='{}'
    S._cached = C()
    ts=[threading.Thread(target=S._credentials) for _ in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    os.environ.pop('GBP_TOKEN_JSON')
    assert len(calls)==1

# 3. concurrent identical posts -> exactly one POST
def test_post_dedup_concurrent(S):
    n=[]
    def fake(method,url,params=None,body=None):
        n.append(1); time.sleep(0.1); return {"name":"p"}
    S._call = fake
    res=[]
    def go():
        try: res.append(S.create_post("1","2","hello",dry_run=False))
        except Exception as e: res.append(e)
    ts=[threading.Thread(target=go) for _ in range(10)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(n)==1

# 4. ambiguous failure (read timeout AFTER send) must NOT release the dedup key
def test_post_timeout_keeps_guard(S):
    with mock.patch.object(S, "_credentials", lambda bad_token=None: mock.Mock(token="t")), \
         mock.patch.object(S.requests, "request", side_effect=requests.ReadTimeout("slow")) as rq:
        with pytest.raises(RuntimeError):
            S.create_post("1","2","hello",dry_run=False)
    # user/client retries
    with mock.patch.object(S, "_credentials", lambda bad_token=None: mock.Mock(token="t")), \
         mock.patch.object(S.requests, "request", return_value=FakeResp(200,{"name":"p"})) as rq:
        with pytest.raises(RuntimeError, match="identical|unknown|Outcome"):
            S.create_post("1","2","hello",dry_run=False)
        assert rq.call_count==0, "retry after ambiguous timeout could double-post"

# 5. dedup key must include CTA/url (different post, same text)
def test_post_key_includes_cta(S):
    S._call = lambda *a, **k: {"name":"p"}
    S.create_post("1","2","Sale!",dry_run=False)
    S.create_post("1","2","Sale!",call_to_action="LEARN_MORE",url="https://a.com",dry_run=False)

# 6. CTA validation
def test_cta_validation(S):
    with pytest.raises(ValueError): S.create_post("1","2","x","LEARN_MORE",None)      # url missing
    with pytest.raises(ValueError): S.create_post("1","2","x","CALL","https://a.com") # url not allowed
    with pytest.raises(ValueError): S.create_post("1","2","x","BOGUS","https://a.com")
    with pytest.raises(ValueError): S.create_post("1","2","x",None,"https://a.com")

# 7. dry_run must validate ids/inputs the same way as the real call
def test_reply_dry_run_validates(S):
    with pytest.raises(ValueError): S.reply_to_review("1","2","../../x","hi")
    with pytest.raises(ValueError): S.reply_to_review("abc","2","r1","hi")
    with pytest.raises(ValueError): S.reply_to_review("1","2","r1","")
    with pytest.raises(ValueError): S.reply_to_review("1","2","r1","x"*4097)

# 8. empty update must not hit the API
def test_empty_update(S):
    S._call = mock.Mock()
    with pytest.raises(ValueError): S.update_location("1", {}, dry_run=False)
    S._call.assert_not_called()

# 9. malformed token env -> RuntimeError (not ValueError which becomes HTTP 400)
def test_bad_env_token(S):
    with mock.patch.dict(os.environ, {"GBP_TOKEN_JSON": "{not json"}):
        with pytest.raises(RuntimeError):
            S._credentials()
