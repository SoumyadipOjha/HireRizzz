"""Company existence check (Wikidata + website), with fake Wikidata answers: no network in tests."""

from __future__ import annotations

from datetime import date

from test_credibility import job, profile

from screening.company_check import CompanyLookup, company_checks

TODAY = date(2026, 9, 27)

ENTITIES = {
    "Q1": {"labels": {"en": {"value": "Infosys limited"}},
           "descriptions": {"en": {"value": "Indian multinational technology company"}},
           "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q4830453"}}}}],
                      "P571": [{"mainsnak": {"datavalue": {"value": {"time": "+1981-07-02T00:00:00Z"}}}}],
                      "P856": [{"mainsnak": {"datavalue": {"value": "https://www.infosys.com/"}}}]}},
    "Q2": {"labels": {"en": {"value": "Infosys Prize"}}, "descriptions": {"en": {"value": "award"}}, "claims": {}},
    "Q3": {"labels": {"en": {"value": "NovaAI Labs"}}, "descriptions": {"en": {"value": "software company"}},
           "claims": {"P571": [{"mainsnak": {"datavalue": {"value": {"time": "+2021-01-01T00:00:00Z"}}}}]}},
    "Q4": {"labels": {"en": {"value": "OldCo"}}, "descriptions": {"en": {"value": "defunct company"}},
           "claims": {"P571": [{"mainsnak": {"datavalue": {"value": {"time": "+1990-01-01T00:00:00Z"}}}}],
                      "P576": [{"mainsnak": {"datavalue": {"value": {"time": "+2012-01-01T00:00:00Z"}}}}],
                      "P856": [{"mainsnak": {"datavalue": {"value": "https://oldco.example/"}}}]}},
}
SEARCH = {"infosys": ["Q2", "Q1"], "novaai labs": ["Q3"], "oldco": ["Q4"]}


def fake_fetch(url):
    from urllib.parse import parse_qs, urlparse

    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    if q["action"] == "wbsearchentities":
        ids = SEARCH.get(q["search"].lower().replace(" limited", ""), [])
        return {"search": [{"id": i, "label": ENTITIES[i]["labels"]["en"]["value"]} for i in ids]}
    return {"entities": {i: ENTITIES[i] for i in q["ids"].split("|")}}


def lookup(website_ok=True, fetch=fake_fetch):
    calls = []

    def ping(url):
        calls.append(url)
        return website_ok
    lk = CompanyLookup(fetch=fetch, ping=ping)
    lk.pings = calls
    return lk


def by_check(flags):
    return {f.check: f for f in flags}


def test_listed_company_with_matching_dates_is_green():
    p = profile([job("Engineer", "Infosys Limited", "2019-06", current=True)])
    [f] = company_checks(p, lookup(), TODAY)
    assert f.level == "green" and "Infosys limited" in f.message and "founded 1981" in f.message
    assert "https://www.infosys.com/ is online" in f.message    # the award "Infosys Prize" was skipped


def test_job_before_the_company_existed_or_after_it_closed_is_red():
    p = profile([job("Principal Engineer", "NovaAI Labs", "2017-01", current=True),
                 job("Developer", "OldCo", "2015-01", "2016-12")])
    got = by_check(company_checks(p, lookup(), TODAY))
    assert got["company_founded_after_role"].level == "red" and "founded in 2021" in got["company_founded_after_role"].message
    assert got["company_dissolved_before_role"].level == "red" and "closed in 2012" in got["company_dissolved_before_role"].message


def test_unlisted_company_is_only_a_check_not_fraud():
    p = profile([job("Engineer", "Kanerika", "2022-01", current=True)])
    [f] = company_checks(p, lookup(), TODAY)
    assert f.level == "amber" and f.check == "company_not_found" and "Small companies" in f.message


def test_website_down_and_lookup_failure_are_checks():
    p = profile([job("Engineer", "Infosys", "2019-06", current=True)])
    [f] = company_checks(p, lookup(website_ok=False), TODAY)
    assert f.level == "amber" and f.check == "company_website_down"

    def broken(url):
        raise OSError("no network")
    [f] = company_checks(p, lookup(fetch=broken), TODAY)
    assert f.level == "amber" and f.check == "company_lookup_failed"


def test_each_company_is_looked_up_once():
    lk = lookup()
    p = profile([job("Engineer", "Infosys", "2022-01", current=True), job("Intern", "Infosys Ltd", "2019-01", "2019-06")])
    company_checks(p, lk, TODAY)
    company_checks(p, lk, TODAY)            # cached across candidates too
    assert lk.pings == ["https://www.infosys.com/"]
