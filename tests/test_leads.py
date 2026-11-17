"""Hiring posts: sources, reading, scoring, acting on them, sharing from the phone."""

import json
from types import SimpleNamespace

import httpx
import pytest

from pipeline import leads as L
from pipeline.ai import client as ai
from tests.test_ai import KEY, FakeClient
from tests.test_autonomy import CHAT, FakeTelegram, pdir, tg  # noqa: F401

CONFIG = {"filters": {"role_keywords": ["software engineer", "backend", "engineer"], "locations": ["New York", "Remote"],
                      "exclude_keywords": ["intern"]},
          "stack": {"languages": {"primary": ["Java", "Python"]}, "frameworks": ["Spring Boot"]}}

HN_THREAD = {"hits": [{"objectID": "900", "title": "Ask HN: Who is hiring? (November 2026)"}]}
HN_COMMENTS = {"hits": [
    {"objectID": "901", "parent_id": 900, "created_at_i": 1763000000, "created_at": "2026-11-03T15:00:00Z",
     "author": "acmecto", "comment_text": "Acme | Senior Backend Engineer (Java) | New York or REMOTE | Visa sponsorship available<p>"
     "We build payments infra. Apply: <a href=\"https://job-boards.greenhouse.io/acme/jobs/123\">link</a>"},
    {"objectID": "902", "parent_id": 901, "created_at_i": 1763000100, "author": "x", "comment_text": "Is this remote?"},
]}
HN_JOBS = {"hits": [{"objectID": "950", "title": "Globex (YC W24) is hiring a founding engineer",
                     "url": "https://jobs.ashbyhq.com/globex/abc", "author": "globex", "created_at": "2026-11-15T10:00:00Z"}]}


def _hn_transport():
    def handler(req):
        tags = req.url.params.get("tags", "")
        if tags == "story,author_whoishiring":
            return httpx.Response(200, json=HN_THREAD)
        if tags.startswith("comment,story_900"):
            return httpx.Response(200, json=HN_COMMENTS if req.url.params.get("page") == "0" else {"hits": []})
        if tags == "job":
            return httpx.Response(200, json=HN_JOBS)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_hn_reads_top_level_postings_and_yc_jobs(tmp_path):
    store = L.LeadStore(tmp_path / "l.db")
    posts = L.hn_posts(_hn_transport(), store)
    ids = [p["id"] for p in posts]
    assert ids == ["hn:901", "hnjob:950"]                       # the reply (902) isn't a posting
    assert "https://job-boards.greenhouse.io/acme/jobs/123" in posts[0]["text"]
    assert store.get("hn_since:900") == "1763000100"            # next time: only newer comments


def test_bluesky_and_links(tmp_path):
    store = L.LeadStore(tmp_path / "l.db")
    post = {"uri": "at://did:plc:abc/app.bsky.feed.post/3kx", "author": {"handle": "cto.bsky.social"},
            "record": {"text": "We're hiring a backend engineer in NYC!", "createdAt": "2026-11-17T12:00:00Z",
                       "facets": [{"features": [{"uri": "https://jobs.lever.co/initech/1"}]}]}}

    def handler(req):
        assert req.url.host == "api.bsky.app" and req.url.params["sort"] == "latest"
        return httpx.Response(200, json={"posts": [post]})
    posts = L.bluesky_posts(httpx.Client(transport=httpx.MockTransport(handler)), store, ["#hiring"])
    assert posts[0]["url"] == "https://bsky.app/profile/cto.bsky.social/post/3kx"
    assert "jobs.lever.co/initech" in posts[0]["text"]


def test_x_stays_under_the_daily_cap(tmp_path):
    store = L.LeadStore(tmp_path / "l.db")
    seen = []

    def handler(req):
        seen.append(req)
        assert req.headers["Authorization"] == "Bearer tok"
        n = int(req.url.params["max_results"])
        return httpx.Response(200, json={"data": [{"id": str(i), "text": "We're hiring engineers", "created_at": "2026-11-17T12:00:00Z"}
                                                  for i in range(n)], "meta": {"newest_id": "99"}})
    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert len(L.x_posts(c, store, "tok", ["hiring"], daily_cap=30)) == 30
    assert store.x_reads_today() == 30 and store.get("x_since_id") == "99"
    assert L.x_posts(c, store, "tok", ["hiring"], daily_cap=30) == [] and len(seen) == 1    # cap reached: no call

    def denied(req):
        return httpx.Response(401, json={})
    with pytest.raises(PermissionError):
        L.x_posts(httpx.Client(transport=httpx.MockTransport(denied)), L.LeadStore(tmp_path / "m.db"), "tok", ["x"], 10)


def test_rules_read_hn_format():
    p = {"source": "hn", "text": "Acme (YC S20) | Senior Backend Engineer | New York, NY | Visa sponsorship available\n"
                                 "Apply https://job-boards.greenhouse.io/acme/jobs/1"}
    o = L.extract_rules(p)[0]
    assert o["company"] == "Acme" and "Backend" in o["role"] and o["visa"] == "yes"
    assert o["apply_url"].startswith("https://job-boards.greenhouse.io/acme")
    q = {"source": "bluesky", "text": "Hiring a senior software engineer, US citizens only. DM me"}
    assert L.extract_rules(q)[0]["visa"] == "no"


def test_process_with_claude_scores_dedupes_and_skips_job_seekers(pdir, tmp_path):  # noqa: F811
    ai.save_key(KEY, pdir)
    store = L.LeadStore(tmp_path / "l.db")
    posts = [{"id": "bsky:1", "source": "bluesky", "url": "u1", "author": "a", "text": "We're hiring a backend engineer, NYC, visa ok"},
             {"id": "bsky:2", "source": "bluesky", "url": "u2", "author": "b", "text": "I'm looking for a job as an engineer #opentowork"},
             {"id": "bsky:3", "source": "bluesky", "url": "u3", "author": "c", "text": "Great weather today"}]
    fake = FakeClient(text=json.dumps({"results": [{"i": 0, "hiring": True, "openings": [
        {"company": "Initech", "role": "Backend Engineer", "location": "New York", "remote": False, "visa": "yes",
         "apply_url": "https://jobs.lever.co/initech/1", "contact": ""}]}]}))
    new = L.process(posts, store, CONFIG, pdir, fake)
    assert len(fake.calls) == 1 and "bsky:2" not in fake.calls[0]["messages"][0]["content"]   # seekers filtered first
    assert [n["company"] for n in new] == ["Initech"] and new[0]["visa"] == "yes" and new[0]["score"] > 50
    assert L.process(posts, store, CONFIG, pdir, fake) == [] and len(fake.calls) == 1        # seen: not re-read


def test_no_sponsorship_scores_low(pdir, tmp_path):  # noqa: F811
    store = L.LeadStore(tmp_path / "l.db")
    p = {"id": "hn:1", "source": "hn", "url": "u", "text": "Acme | Backend Engineer | NYC | No visa sponsorship"}
    yes = {"id": "hn:2", "source": "hn", "url": "u", "text": "Globex | Backend Engineer | NYC | Visa sponsorship available"}
    a, b = L.process([p, yes], store, CONFIG, pdir)                      # rules (no AI key)
    assert a["score"] < b["score"] and a["visa"] == "no"


def test_watch_adds_the_board_behind_the_link():
    added = []
    reg = SimpleNamespace(add=lambda p, s, src: added.append((p, s, src)) or True)
    assert L.watch({"apply_url": "https://job-boards.greenhouse.io/acme/jobs/1"}, registry=reg) == "greenhouse/acme"
    assert added == [("greenhouse", "acme", "lead")]
    # No link: look the company up
    got = L.watch({"company": "Initech", "snippet": "hiring!"}, registry=reg, resolve=lambda n: [("lever", "initech")])
    assert got == "lever/initech"
    assert L.watch({"company": "", "snippet": ""}, registry=reg) == ""


def test_shared_screenshot_is_read_by_claude(pdir, tmp_path):  # noqa: F811
    ai.save_key(KEY, pdir)
    store = L.LeadStore(tmp_path / "l.db")

    class TwoReplies(FakeClient):
        n = 0

        def __init__(self):
            super().__init__(text="")
            self.messages = self

        def create(self, **kw):
            self.calls.append(kw)
            TwoReplies.n += 1
            text = ("Jane Doe, Eng Manager at Hooli\nWe're hiring backend engineers in NYC. Apply: https://jobs.ashbyhq.com/hooli/1"
                    if TwoReplies.n == 1 else json.dumps({"results": [{"i": 0, "hiring": True, "openings": [
                        {"company": "Hooli", "role": "Backend Engineer", "location": "NYC", "visa": "unknown",
                         "apply_url": "https://jobs.ashbyhq.com/hooli/1"}]}]}))
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=10,
                                                         cache_read_input_tokens=0, cache_creation_input_tokens=0))
    fake = TwoReplies()
    found = L.add_shared("", store=store, config=CONFIG, private_dir=pdir, image=b"\xff\xd8jpeg", ai_client=fake)
    assert fake.calls[0]["messages"][0]["content"][0]["type"] == "image"
    assert found[0]["company"] == "Hooli" and found[0]["source"] == "manual"
    with pytest.raises(ValueError):
        L.add_shared("https://linkedin.com/posts/x", store=store, config=CONFIG, private_dir=pdir)


def test_telegram_forwarded_post_and_lead_buttons(tg, pdir):  # noqa: F811
    t, fake = tg
    t.connect("123:ABC")
    fake.add(message={"message_id": 1, "chat": CHAT, "text": "/start"})
    t.wait_for_start(5)
    shared, acted = [], []
    fake.add(message={"message_id": 2, "chat": CHAT, "text": "Acme is hiring backend engineers! https://x.com/a/1"})
    t.poll_once(lambda j, a: acted.append((j, a)) or "ok", on_share=lambda text, img: shared.append((text, img)) or "Got it")
    assert shared == [("Acme is hiring backend engineers! https://x.com/a/1", None)]
    assert any("Got it" in m.get("text", "") for m in fake.sent)
    t.notify_lead({"id": "hn:1#0", "source": "hn", "company": "Acme", "role": "Backend Engineer", "score": 82,
                   "visa": "yes", "url": "https://news.ycombinator.com/item?id=1", "snippet": "Acme | Backend"})
    msg = fake.sent[-1]
    assert "Acme · Backend Engineer" in msg["text"] and "visa" in msg["text"]
    cb = msg["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    fake.add(callback_query={"id": "c", "data": cb, "message": {"message_id": 5, "chat": CHAT}})
    t.poll_once(lambda j, a: acted.append((j, a)) or "ok")
    assert acted == [("hn:1#0", "lead_watch")]


def test_fetch_reports_source_errors_without_stopping(pdir, tmp_path):  # noqa: F811
    def handler(req):
        if req.url.host == "api.bsky.app":
            raise httpx.ConnectError("down")
        return _hn_transport()._transport.handle_request(req)
    rep = L.fetch(sources=["hn", "bluesky"], private_dir=pdir, store=L.LeadStore(tmp_path / "l.db"),
                  client=httpx.Client(transport=httpx.MockTransport(handler)), config=CONFIG)
    assert rep["posts"]["hn"] == 2 and "bluesky" in rep["errors"]
    assert any(n["company"] == "Acme" for n in rep["new"])


def test_bluesky_refusal_is_an_error_and_app_password_signs_in(tmp_path):
    store = L.LeadStore(tmp_path / "l.db")

    def refused(req):
        return httpx.Response(403, json={})
    with pytest.raises(RuntimeError, match="403"):
        L.bluesky_posts(httpx.Client(transport=httpx.MockTransport(refused)), store, ["#hiring"])

    def with_login(req):
        if req.url.host == "api.bsky.app":
            return httpx.Response(403, json={})
        if req.url.path.endswith("createSession"):
            return httpx.Response(200, json={"accessJwt": "jwt"})
        assert req.headers["Authorization"] == "Bearer jwt"
        return httpx.Response(200, json={"posts": [{"uri": "at://d/app.bsky.feed.post/1", "author": {"handle": "a"},
                                                    "record": {"text": "We're hiring engineers", "createdAt": "2026-11-17T00:00:00Z"}}]})
    posts = L.bluesky_posts(httpx.Client(transport=httpx.MockTransport(with_login)), store, ["#hiring"], "me.bsky.social", "abcd-efgh-ijkl-mnop")
    assert len(posts) == 1
