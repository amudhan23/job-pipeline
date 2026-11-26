"""Interview prep pack: research with web search, a pack from the profile,
every story checked against it, thank-you notes."""
import json

import pytest

from pipeline import interview
from pipeline.ai import client as ai
from pipeline.resume import writer as w
from tests.test_ai import KEY
from tests.test_profile_writer import PROFILE

JOB = {"id": "gh:acme:1", "company": "Acme Payments", "title": "Backend Engineer", "url": "https://x/1",
       "description": "Payments infrastructure team. Java, Spring Boot, Redis, high-throughput APIs. " * 5}


class Scripted:
    """Answers each call from a queue; records requests."""
    def __init__(self, replies):
        self.replies, self.calls, self.messages = list(replies), [], self

    def create(self, **kw):
        from types import SimpleNamespace
        self.calls.append(kw)
        text = self.replies.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(text=text)],
                               usage=SimpleNamespace(input_tokens=2000, output_tokens=900, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0,
                                                     server_tool_use=SimpleNamespace(web_search_requests=4)),
                               stop_reason="end_turn")


@pytest.fixture
def pdir(tmp_path, monkeypatch):
    from pipeline import applicant
    d = tmp_path / "private"
    monkeypatch.setattr(applicant, "PRIVATE_DIR", d)
    ai.save_key(KEY, d)
    w.save_profile(PROFILE, d)
    return d


RESEARCH = json.dumps({"company": "Acme runs a payments ledger for marketplaces.", "recent": ["Oct 2026: Series B"],
                       "stack": ["Java", "Postgres", "Kafka"],
                       "process": [{"round": "Phone screen", "format": "45 min coding", "themes": ["hash maps", "APIs"]},
                                   {"round": "Onsite", "format": "design + behavioural", "themes": ["ledger design"]}],
                       "sources": ["https://www.glassdoor.com/acme", "not-a-url"], "unknown": ""})
PACK = json.dumps({"headline": "Can you own a service under load and explain your choices.",
                   "rounds": [{"round": "Phone screen", "format": "45 min coding",
                               "questions": [{"q": "Design a rate limiter", "kind": "technical",
                                              "answer": "Token bucket per key in Redis; mention the IBM caching work.",
                                              "facts": ["Redis caching at IBM cut API response times 40%"]},
                                             {"q": "Tell me about a production incident", "kind": "behavioural",
                                              "answer": "Situation. Action. Result.",
                                              "facts": ["Led the Kubernetes migration of 200 services"]}]}],
                   "stories": [{"name": "Caching win", "from": "IBM", "situation": "Slow APIs at IBM",
                                "action": "Added Redis caching and optimized PostgreSQL queries",
                                "result": "API response times down 40%", "use_for": ["performance"]}],
                   "gaps": [{"probe": "No Kafka in your profile", "answer": "True; closest is batch jobs with Jenkins."}],
                   "ask_them": ["How is on-call shared?"], "plan": [{"when": "Day 1", "do": "Two rate-limiter drills"}]})


def test_prep_pack_is_researched_written_and_fact_checked(pdir, tmp_path):
    fake = Scripted([RESEARCH, PACK])
    folder = tmp_path / "app"
    data = interview.prep(JOB, folder, pdir, client=fake, notes="Recruiter said two rounds.")
    # research used web search; the pack call carried the profile first (cached) and the research
    assert fake.calls[0]["tools"][0]["type"].startswith("web_search")
    assert "<career_profile>" in fake.calls[1]["system"][0]["text"] and "<research>" in fake.calls[1]["messages"][0]["content"]
    assert "Recruiter said two rounds" in fake.calls[1]["messages"][0]["content"]
    assert data["research"]["sources"] == ["https://www.glassdoor.com/acme"]        # non-URLs dropped
    assert data["pack"]["headline"].startswith("Can you own")
    # the invented Kubernetes fact is flagged, the real Redis one is not
    qs = data["pack"]["rounds"][0]["questions"]
    assert "unsupported" not in qs[0] and qs[1]["unsupported"] == ["Led the Kubernetes migration of 200 services"]
    assert data["flagged"] and data["flagged"][0]["where"].startswith("Tell me about")
    assert "unsupported" not in data["pack"]["stories"][0]
    assert data["cost"] > 0 and (folder / "prep.json").exists()
    assert interview.load(folder)["pack"]["gaps"][0]["probe"].startswith("No Kafka")


def test_prep_still_written_when_research_fails(pdir, tmp_path):
    fake = Scripted(["not json at all", PACK])
    data = interview.prep(JOB, tmp_path / "app", pdir, client=fake)
    assert data["research"]["unknown"].startswith("research failed") and data["pack"]["rounds"]


def test_thank_you_note_is_saved_with_the_pack(pdir, tmp_path):
    folder = tmp_path / "app"
    interview.prep(JOB, folder, pdir, client=Scripted([RESEARCH, PACK]))
    fake = Scripted([json.dumps({"subject": "Thank you, Priya", "body": "Thanks for walking me through the ledger — today."})])
    note = interview.thank_you(JOB, folder, "Priya", "We discussed the ledger and idempotency.", pdir, client=fake)
    assert note["subject"] == "Thank you, Priya" and "—" not in note["body"]
    assert "idempotency" in fake.calls[0]["messages"][0]["content"]
    assert interview.load(folder)["thank_you"][0]["interviewer"] == "Priya"


def test_linkedin_text_is_written_from_the_profile_and_checked(pdir):
    from pipeline import linkedin
    fake = Scripted([json.dumps({
        "headline": "Software Engineer | Java, Spring Boot, PostgreSQL | 3.5 years at IBM",
        "about": "I build backend services in Java and Spring Boot. At IBM I added Redis caching that cut API response times 40%. "
                 "I once led a Kubernetes migration of 200 services. I am looking for backend roles in New York.",
        "skills": ["Java", "Spring Boot", "PostgreSQL", "Redis"], "open_to": ["Software Engineer", "Backend Engineer"]})])
    d = linkedin.write(pdir, client=fake, roles=["Software Engineer", "Backend Engineer"])
    assert d["headline"].startswith("Software Engineer |") and d["skills"][0] == "Java" and d["open_to"][1] == "Backend Engineer"
    assert len(d["flagged"]) == 1 and "Kubernetes" in d["flagged"][0]["line"]       # the invented sentence
    assert "Target roles: Software Engineer, Backend Engineer" in fake.calls[0]["messages"][0]["content"]
    assert linkedin.load(pdir)["about"] == d["about"]
