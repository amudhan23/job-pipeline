"""The self-test's live-job fetch, per board platform (network faked)."""
from pipeline import doctor


class FakeHttp:
    def __init__(self, payloads):
        self.payloads = payloads

    def get(self, url, params=None):
        class R:
            def __init__(self, data):
                self.status_code = 200 if data is not None else 404
                self._data = data

            def json(self):
                return self._data
        for key, data in self.payloads.items():
            if key in url:
                return R(data)
        return R(None)


def test_live_job_from_each_platform():
    c = FakeHttp({
        "greenhouse.io/v1/boards/acme": {"jobs": [{"id": 7, "title": "Software Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/7",
                                                   "location": {"name": "NYC"}, "content": "<p>Build.</p>", "company_name": "Acme"}]},
        "lever.co/v0/postings/beta": [{"id": "abc", "text": "Backend Engineer", "hostedUrl": "https://jobs.lever.co/beta/abc",
                                       "categories": {"location": "Remote"}, "descriptionPlain": "Go and Postgres."}],
        "ashbyhq.com/posting-api/job-board/gamma": {"jobs": [{"id": "u1", "title": "Designer", "jobUrl": "https://jobs.ashbyhq.com/gamma/u1", "isListed": True},
                                                             {"id": "u2", "title": "Founding Engineer", "jobUrl": "https://jobs.ashbyhq.com/gamma/u2",
                                                              "location": "SF", "descriptionPlain": "Agents.", "isListed": True}]},
        "workable.com/api/v1/widget/accounts/delta": {"jobs": [{"shortcode": "AB12", "title": "Platform Engineer",
                                                               "url": "https://apply.workable.com/delta/j/AB12/",
                                                               "location": {"city": "London"}, "description": "<p>Kubernetes.</p>"}]},
    })
    gh = doctor._live_job("greenhouse", "acme", c)
    assert gh["id"] == "gh-acme-7" and gh["platform"] == "greenhouse" and gh["company"] == "Acme"
    lv = doctor._live_job("lever", "beta", c)
    assert lv["url"] == "https://jobs.lever.co/beta/abc" and lv["location"] == "Remote" and lv["title"] == "Backend Engineer"
    ab = doctor._live_job("ashby", "gamma", c)
    assert ab["title"] == "Founding Engineer" and ab["url"].endswith("/u2")      # an engineering role is preferred
    assert doctor._live_job("lever", "nobody", c) is None and doctor._live_job("workday", "x", c) is None

    # the application form page per platform, as the filler opens it
    from pipeline.apply.filler import application_url
    assert application_url(gh) == "https://job-boards.greenhouse.io/acme/jobs/7"
    assert application_url(lv) == "https://jobs.lever.co/beta/abc/apply"
    assert application_url(ab) == "https://jobs.ashbyhq.com/gamma/u2/application"
    wk = doctor._live_job("workable", "delta", c)
    assert wk["id"] == "wk-delta-AB12" and wk["location"] == "London" and "Kubernetes" in wk["description"]
    assert application_url(wk) == "https://apply.workable.com/delta/j/AB12/apply/"
