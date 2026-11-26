"""Outreach: plan, contacts, email finding, drafts, follow-up schedule."""

import json
from datetime import datetime, timedelta

import httpx
import pytest

from pipeline import outreach as o
from pipeline.ai import client as ai
from pipeline.resume import writer as w
from tests.test_ai import KEY, FakeClient
from tests.test_profile_writer import PROFILE

JOB = {"id": "gh:acme:1", "company": "Acme Payments, Inc.", "title": "Backend Engineer",
       "url": "https://boards.greenhouse.io/acme/jobs/1", "status": "applied",
       "description": "Payments infrastructure team. Java, Spring Boot, Redis, high-throughput APIs."}


@pytest.fixture
def pdir(tmp_path, monkeypatch):
    from pipeline import applicant
    monkeypatch.setattr(applicant, "PRIVATE_DIR", tmp_path)
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    return tmp_path


def test_domain_and_name_guesses():
    assert o.guess_domain("Acme Payments, Inc.") == "acmepayments.com"
    assert o.guess_domain("Zoë Labs") == "zoe.com"
    assert o.name_parts("Dr. José García Jr.") == ("jose", "garcia")
    g = o.guesses("Priya Raman", "acme.com")
    assert g[:3] == ["priya.raman@acme.com", "priya@acme.com", "praman@acme.com"]
    assert o.guesses("Cher", "acme.com") == ["cher@acme.com"]           # no last name: only {first}


def test_plan_without_ai_gives_manager_titles_and_links(pdir):
    rec = o.plan(JOB, pdir)
    assert rec["plan"]["by"] == "rules" and "Engineering Manager" in rec["plan"]["titles"]
    link = rec["plan"]["links"][0]
    assert link["linkedin"].startswith("https://www.linkedin.com/search/results/people/?keywords=")
    assert "site%3Alinkedin.com%2Fin" in link["google"]
    assert rec["domain"] == "acmepayments.com"


def test_plan_with_ai(pdir):
    ai.save_key(KEY, pdir)
    fake = FakeClient(text=json.dumps({"team": "Payments Infrastructure",
                                       "titles": ["Engineering Manager, Payments"], "why": "Owns the APIs.",
                                       "angle": "Redis caching cut latency 40%"}))
    rec = o.plan(JOB, pdir, client=fake)
    assert rec["plan"]["team"] == "Payments Infrastructure" and rec["plan"]["by"] == "ai"
    assert fake.calls[0]["model"] == "claude-haiku-5-5"


def test_contacts_validate_and_track(pdir):
    with pytest.raises(ValueError):
        o.add_contact(JOB, {"name": ""}, pdir)
    with pytest.raises(ValueError):
        o.add_contact(JOB, {"name": "A B", "linkedin": "https://evil.example/in/x"}, pdir)
    rec = o.add_contact(JOB, {"name": "Priya Raman", "title": "EM", "linkedin": "linkedin.com/in/priya"}, pdir)
    c = rec["contacts"][0]
    assert o.next_step(c)["step"] == "draft"
    c["drafts"] = {"email": "hi"}
    assert o.next_step(c)["step"] == "send"
    sent = datetime(2026, 11, 1, 9, 0)
    c["sent_at"] = sent.isoformat()
    s = o.next_step(c, now=sent + timedelta(days=2))
    assert s["step"] == "followup1" and not s["overdue"]
    assert o.next_step(c, now=sent + timedelta(days=4, minutes=1))["overdue"]
    c["followups_sent"] = ["x", "y"]
    assert o.next_step(c)["step"] == "done"
    c["replied_at"] = "now"
    assert o.next_step(c)["step"] == "replied"


def test_due_followups_and_marks(pdir):
    rec = o.add_contact(JOB, {"name": "Priya Raman"}, pdir)
    cid = rec["contacts"][0]["id"]
    data = o.load(pdir)
    data[JOB["id"]]["contacts"][0]["drafts"] = {"email": "hi"}
    o._save(data, pdir)
    o.update_contact(JOB, cid, "sent", pdir)
    later = datetime.now() + timedelta(days=5)
    due = o.due_followups(pdir, now=later)
    assert len(due) == 1 and due[0]["label"] == "Follow-up 1"
    o.update_contact(JOB, cid, "followup", pdir)
    assert not o.due_followups(pdir, now=later)
    o.update_contact(JOB, cid, "replied", pdir)
    assert o.overview(pdir)[0]["contacts"][0]["next"]["step"] == "replied"
    o.update_contact(JOB, cid, "delete", pdir)
    assert o.load(pdir)[JOB["id"]]["contacts"] == []


def test_find_email_guess_without_hunter(pdir):
    rec = o.add_contact(JOB, {"name": "Priya Raman"}, pdir)
    rec = o.find_email(JOB, rec["contacts"][0]["id"], pdir, domain="https://www.acme.com/about")
    c = rec["contacts"][0]
    assert rec["domain"] == "acme.com"
    assert c["email"] == "priya.raman@acme.com" and c["email_status"] == "guessed"
    assert "not verified" in c["email_note"]


def _hunter(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_find_email_with_hunter(pdir):
    o.save_hunter_key("a" * 40, pdir)
    rec = o.add_contact(JOB, {"name": "Priya Raman"}, pdir)
    cid = rec["contacts"][0]["id"]

    def finder(req):
        assert req.url.path == "/v2/email-finder" and req.url.params["first_name"] == "priya"
        return httpx.Response(200, json={"data": {"email": "praman@acme.com", "score": 94,
                                                  "verification": {"status": "valid"}}})
    c = o.find_email(JOB, cid, pdir, client=_hunter(finder), domain="acme.com")["contacts"][0]
    assert c["email"] == "praman@acme.com" and c["email_status"] == "verified" and "94%" in c["email_note"]

    def pattern_only(req):
        if req.url.path == "/v2/email-finder":
            return httpx.Response(200, json={"data": {"email": None}})
        return httpx.Response(200, json={"data": {"pattern": "{f}{last}"}})
    c = o.find_email(JOB, cid, pdir, client=_hunter(pattern_only))["contacts"][0]
    assert c["email"] == "praman@acme.com" and c["email_status"] == "guessed" and "pattern" in c["email_note"]

    def limited(req):
        return httpx.Response(429, json={})
    c = o.find_email(JOB, cid, pdir, client=_hunter(limited))["contacts"][0]
    assert c["email"] == "priya.raman@acme.com" and "used up" in c["email_note"]


def test_hunter_key_validation(pdir):
    with pytest.raises(ValueError):
        o.save_hunter_key("bad key!", pdir)
    o.save_hunter_key("b" * 40, pdir)
    assert o.hunter_key(pdir) == "b" * 40
    assert oct((pdir / o.HUNTER_FILE).stat().st_mode)[-3:] == "600"
    o.save_hunter_key("", pdir)
    assert o.hunter_key(pdir) == ""


def test_draft_strips_dashes_and_flags_invented_facts(pdir):
    ai.save_key(KEY, pdir)
    w.save_profile(PROFILE, pdir)
    rec = o.add_contact(JOB, {"name": "Priya Raman", "title": "Engineering Manager", "email": "p@acme.com"}, pdir)
    cid = rec["contacts"][0]["id"]
    fake = FakeClient(text=json.dumps({
        "subject": "Backend Engineer, payments",
        "email": "Hi Priya — I applied for the Backend Engineer role. At IBM I added Redis caching that cut "
                 "API latency 40%. I also scaled Kubernetes clusters to 900 nodes. Open to a 15-minute chat?",
        "linkedin": "x" * 400, "followup1": "Following up.", "followup2": "Closing the loop."}))
    rec = o.draft(JOB, cid, pdir, client=fake)
    d = rec["contacts"][0]["drafts"]
    assert "—" not in d["email"] and len(d["linkedin"]) <= 300
    assert any("900" in x or "Kubernetes" in x for x in d["warnings"])
    assert not any("40%" in x for x in d["warnings"])
    assert "<career_profile>" in fake.calls[0]["system"][1]["text"]
    assert "already applied" in fake.calls[0]["messages"][0]["content"]
    links = o.overview(pdir)[0]["contacts"][0]["compose"]["email"]
    assert links["mailto"].startswith("mailto:p%40acme.com?subject=Backend")
    assert links["gmail"].startswith("https://mail.google.com/mail/?view=cm")


def test_outreach_api(pdir, tmp_path):
    from pipeline.dashboard.routes import ApiMixin

    class Tracker:
        def get_job(self, jid):
            return JOB if jid == JOB["id"] else None

    class H(ApiMixin):
        def _pdir(self):
            return pdir

        def _tracker(self):
            return Tracker()

    h = H()
    rec, code = h.api_post(f"/api/outreach/{JOB['id']}/plan", {})
    assert code == 200 and rec["plan"]["titles"]
    rec, code = h.api_post(f"/api/outreach/{JOB['id']}/contacts", {"name": "Priya Raman"})
    cid = rec["contacts"][0]["id"]
    rec, code = h.api_post(f"/api/outreach/{JOB['id']}/contacts/{cid}/email", {"domain": "acme.com"})
    assert rec["contacts"][0]["email"] == "priya.raman@acme.com"
    body, code = h.api_post(f"/api/outreach/{JOB['id']}/contacts/{cid}/draft", {})
    assert code == 400 and "Anthropic key" in body["error"]
    body, code = h.api_post("/api/outreach/nope/plan", {})
    assert code == 404
    body, code = h.api_post(f"/api/outreach/{JOB['id']}/contacts/deadbeef/sent", {})
    assert code == 404
    body, code = h.api_get("/api/outreach", {})
    assert code == 200 and body["records"][0]["company"] == JOB["company"] and body["hunter"] is False
    body, code = h.api_post("/api/career-profile", {"text": PROFILE})
    assert body["ready"] and "Kubernetes" in body["never_claim"] and body["confirm"] == 3


def test_find_people_searches_the_web_and_keeps_only_sourced_people(pdir):
    """The finder asks Claude to search public pages (Anthropic's web-search
    tool) and keeps only named people with a URL; you accept one to make
    them a contact. Never touches LinkedIn with your login."""
    ai.save_key(KEY, pdir)
    w.save_profile(PROFILE, pdir)
    fake = FakeClient(text=json.dumps({"people": [
        {"name": "Priya Raman", "title": "Engineering Manager, Payments", "url": "https://www.linkedin.com/in/priyaraman",
         "evidence": "Profile: leads Payments Infrastructure at Acme since 2024", "kind": "manager", "confidence": "high"},
        {"name": "Ghost Person", "title": "CTO", "url": "", "evidence": "", "kind": "manager", "confidence": "low"},
        {"name": "Sam Lee", "title": "Technical Recruiter", "url": "https://acme.com/team", "evidence": "Team page",
         "kind": "recruiter", "confidence": "medium"}],
        "note": "Payments team page lists Priya as the lead."}))
    o.plan(JOB, pdir, client=fake)
    rec = o.find_people(JOB, pdir, client=fake)
    people = rec["candidates"]["people"]
    assert [p["name"] for p in people] == ["Priya Raman", "Sam Lee"]          # no URL, no person
    assert people[0]["confidence"] == "high" and rec["candidates"]["note"].startswith("Payments")
    call = fake.calls[-1]
    assert call["tools"][0]["type"].startswith("web_search") and call["tools"][0]["max_uses"] == 5
    assert "Acme" in call["messages"][0]["content"] and "Engineering Manager" in call["messages"][0]["content"]
    rec = o.accept_candidate(JOB, 0, pdir)
    c = rec["contacts"][0]
    assert c["name"] == "Priya Raman" and c["linkedin"] == "https://www.linkedin.com/in/priyaraman" and c["kind"] == "manager"
    with pytest.raises(KeyError):
        o.accept_candidate(JOB, 7, pdir)


def test_schools_from_details_and_profile(pdir):
    from pipeline import applicant
    applicant.save({"school": "New York University"}, pdir)
    w.save_profile(PROFILE + "\n## Education\n- M.S. Computer Science, New York University (Courant Institute)\n- B.Tech, IIT Mandi\n", pdir)
    s = o.schools(pdir)
    assert s[0] == "New York University" and "IIT Mandi" in s and len(s) == len(set(x.lower() for x in s))


def test_send_window_is_tue_to_thu_mornings():
    assert o.send_window(datetime(2026, 11, 25, 9, 0))["now"] is True          # Wednesday 9am
    w_ = o.send_window(datetime(2026, 11, 27, 15, 0))                           # Friday afternoon
    assert w_["now"] is False and w_["next"].startswith("2026-12-01T08:30") and "Tuesday" in w_["label"]
    c = {"drafts": {"email": "hi"}, "sent_at": None, "followups_sent": []}
    assert "window" in o.next_step(c, datetime(2026, 11, 28, 12, 0))


def test_email_ladder_hunter_then_apollo_then_verified_pattern(pdir):
    """Low-confidence Hunter guess → Apollo → company pattern, each verified
    when it can be; the runner-up forms are kept for you to decide."""
    o.save_hunter_key("a" * 40, pdir)
    o.save_apollo_key("apollo-key-1234567890", pdir)
    rec = o.add_contact(JOB, {"name": "Priya Raman"}, pdir)
    cid = rec["contacts"][0]["id"]

    def apollo_reveals(req):
        if req.url.path == "/v2/email-finder":
            return httpx.Response(200, json={"data": {"email": "p.raman@acme.com", "score": 35}})
        if req.url.path == "/api/v1/people/match":
            assert req.headers["x-api-key"].startswith("apollo") and b"priya" in req.content
            return httpx.Response(200, json={"person": {"email": "priya.r@acme.com", "email_status": "verified"}})
        if req.url.path == "/v2/email-verifier":
            return httpx.Response(200, json={"data": {"status": "valid"}})
        return httpx.Response(404)
    c = o.find_email(JOB, cid, pdir, client=_hunter(apollo_reveals), domain="acme.com")["contacts"][0]
    assert c["email"] == "priya.r@acme.com" and c["email_status"] == "verified"
    assert "35%" in c["email_note"] and "Apollo" in c["email_note"] and c["email_alternates"] == ["p.raman@acme.com"]

    def apollo_masked(req):
        if req.url.path == "/v2/email-finder":
            return httpx.Response(200, json={"data": {"email": None}})
        if req.url.path == "/api/v1/people/match":
            return httpx.Response(200, json={"person": {"email": "email_not_unlocked@domain.com"}})
        if req.url.path == "/v2/domain-search":
            return httpx.Response(200, json={"data": {"pattern": "{first}.{last}"}})
        if req.url.path == "/v2/email-verifier":
            ok = req.url.params["email"] == "priya.raman@acme.com"
            return httpx.Response(200, json={"data": {"status": "valid" if ok else "invalid"}})
        return httpx.Response(404)
    c = o.find_email(JOB, cid, pdir, client=_hunter(apollo_masked))["contacts"][0]
    assert c["email"] == "priya.raman@acme.com" and c["email_status"] == "verified"
    assert "masked" in c["email_note"] and "Hunter verified it" in c["email_note"]
    assert "priya@acme.com" in c["email_alternates"]

    with pytest.raises(ValueError):
        o.save_apollo_key("nope", pdir)
