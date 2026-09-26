"""Company existence check for the employers on a resume.

Free, legal sources only:
* Wikidata (the open database behind Wikipedia, public API): is the company listed,
  when was it founded / dissolved, what is its official website.
* The official website itself: is it reachable.

MCA (India's company registry) has no free API and its portal is CAPTCHA-protected,
so official MCA data needs a paid KYC API; `CompanyLookup` is the place to plug one in.

Absence from public records is NOT evidence of fraud (small companies and startups
are rarely on Wikidata): "not found" is only an amber "check" flag. Red flags are
reserved for contradictions: a job that starts before the company was founded or
after it closed.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
USER_AGENT = "HireRizz/1.0 (resume company existence check)"
TIMEOUT = 8

# Wikidata "instance of" classes that mean some kind of business / organisation.
BUSINESS_CLASSES = {
    "Q4830453", "Q783794", "Q6881511", "Q891723", "Q161726", "Q1058914", "Q658255", "Q167037", "Q43229",
    "Q1589009", "Q210167", "Q18388277", "Q22687", "Q327333", "Q2085381", "Q1762059", "Q740752", "Q3918",
    "Q15911314", "Q507619", "Q1331793", "Q7275", "Q1664720", "Q17084016",
}
BUSINESS_WORDS = re.compile(r"\b(company|corporation|business|conglomerate|bank|firm|enterprise|startup|"
                            r"subsidiary|multinational|consult\w*|services|software|technology|manufacturer|"
                            r"retailer|university|institute|government|agency|organi[sz]ation)\b", re.I)

log = logging.getLogger("screening")


@dataclass
class CompanyInfo:
    query: str
    found: bool
    label: str | None = None
    description: str | None = None
    wikidata_id: str | None = None
    founded_year: int | None = None
    dissolved_year: int | None = None
    website: str | None = None
    website_ok: bool | None = None       # None = not checked (no website known)
    error: str | None = None             # lookup failed (network): say so instead of guessing

    @property
    def source(self) -> str:
        return f"Wikidata {self.wikidata_id}" if self.wikidata_id else "Wikidata"


_pace_lock = threading.Lock()
_last_call = [0.0]
MIN_GAP_SECONDS = 1.0          # be polite to Wikidata: at most ~1 request per second from this server


def _get_json(url: str, retries: int = 3) -> dict:
    """GET JSON from Wikidata, spaced out, retrying 429 'Too Many Requests' after the wait it asks for."""
    import time

    for attempt in range(retries + 1):
        with _pace_lock:
            wait = _last_call[0] + MIN_GAP_SECONDS - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == retries:
                raise
            retry_after = e.headers.get("Retry-After", "")
            time.sleep(min(30.0, float(retry_after)) if retry_after.isdigit() else 2.0 * 2 ** attempt)
    raise RuntimeError("unreachable")


def _website_up(url: str) -> bool:
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.status < 500
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code not in (405, 403):
                return True           # the site answered (a 404 page still means it exists)
            if e.code in (403, 405):
                continue              # some sites refuse HEAD or bots: try GET, then accept an answer
            return False
        except Exception:
            return False
    return True


def _year(claims: dict, prop: str) -> int | None:
    for c in claims.get(prop, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and isinstance(v.get("time"), str):
            m = re.match(r"^[+-]?(\d{4})", v["time"])
            if m:
                return int(m.group(1))
    return None


@dataclass
class CompanyLookup:
    """Looks companies up (cached per name). `fetch` / `ping` are injectable for tests and for a
    future paid MCA provider."""
    fetch: callable = _get_json
    ping: callable = _website_up
    _cache: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def lookup(self, name: str) -> CompanyInfo:
        from .credibility import norm_company, same_company

        key = norm_company(name)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        info = self._lookup(name, same_company)
        if info.error is None:
            with self._lock:
                self._cache[key] = info
        return info

    def _lookup(self, name: str, same_company) -> CompanyInfo:
        try:
            hits = self.fetch(f"{WIKIDATA_API}?" + urllib.parse.urlencode({
                "action": "wbsearchentities", "search": name, "language": "en", "type": "item", "limit": 7,
                "format": "json"})).get("search", [])
            candidates = [h for h in hits if same_company(name, h.get("label"))
                          or any(same_company(name, a) for a in h.get("aliases", []) or [])]
            if not candidates:
                return CompanyInfo(query=name, found=False)
            ents = self.fetch(f"{WIKIDATA_API}?" + urllib.parse.urlencode({
                "action": "wbgetentities", "ids": "|".join(h["id"] for h in candidates[:5]),
                "props": "claims|labels|descriptions", "languages": "en", "format": "json"})).get("entities", {})
        except Exception as e:
            log.warning("company check: lookup for %r failed: %s", name, e)
            return CompanyInfo(query=name, found=False, error=f"{type(e).__name__}: {e}")

        for h in candidates:                   # keep search order (best match first)
            e = ents.get(h["id"]) or {}
            claims = e.get("claims", {})
            classes = {c.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
                       for c in claims.get("P31", [])}
            desc = (e.get("descriptions", {}).get("en") or {}).get("value") or h.get("description") or ""
            if not (classes & BUSINESS_CLASSES or BUSINESS_WORDS.search(desc)):
                continue                       # e.g. "Infosys Prize" (an award), a person, a place
            site = next((c["mainsnak"]["datavalue"]["value"] for c in claims.get("P856", [])
                         if c.get("mainsnak", {}).get("datavalue")), None)
            return CompanyInfo(query=name, found=True, wikidata_id=h["id"],
                               label=(e.get("labels", {}).get("en") or {}).get("value") or h.get("label"),
                               description=desc or None, founded_year=_year(claims, "P571"),
                               dissolved_year=_year(claims, "P576"), website=site,
                               website_ok=self.ping(site) if site else None)
        return CompanyInfo(query=name, found=False)


_default = CompanyLookup()


def default_lookup() -> CompanyLookup:
    return _default


def company_checks(profile, lookup: CompanyLookup, today=None) -> list:
    """Flags for every distinct employer on the resume."""
    from datetime import date

    from .credibility import _role, norm_company, span
    from .schemas import CredibilityFlag

    today = today or date.today()
    flags, seen = [], set()
    for w in profile.work_experience:
        key = norm_company(w.company)
        if not key or key in seen:
            continue
        seen.add(key)
        roles = [x for x in profile.work_experience if norm_company(x.company) == key]
        info = lookup.lookup(w.company)
        if info.error:
            flags.append(CredibilityFlag(level="amber", check="company_lookup_failed",
                                         message=f"Couldn't check {w.company} in public records right now "
                                                 "(network problem). Run the checks again later.",
                                         resume=_role(w, today)))
            continue
        if not info.found:
            flags.append(CredibilityFlag(
                level="amber", check="company_not_found",
                message=f"Couldn't find {w.company} in public company records. Small companies and startups often "
                        "aren't listed: check its website or MCA record.",
                resume=_role(w, today), linkedin=None))
            continue
        where = f"{info.label} ({info.source}{': ' + info.description if info.description else ''})"
        problems = []
        for r in roles:
            s, e = span(r, today)
            if info.founded_year and s is not None and s // 12 < info.founded_year - 1:
                problems.append(CredibilityFlag(
                    level="red", check="company_founded_after_role",
                    message=f"The role at {w.company} starts in {s // 12}, but {info.label} was founded in "
                            f"{info.founded_year}.", resume=_role(r, today), linkedin=None))
            if info.dissolved_year and s is not None and s // 12 > info.dissolved_year:
                problems.append(CredibilityFlag(
                    level="red", check="company_dissolved_before_role",
                    message=f"The role at {w.company} starts in {s // 12}, but {info.label} closed in "
                            f"{info.dissolved_year}.", resume=_role(r, today), linkedin=None))
        if problems:
            flags += problems
            continue
        if info.website and info.website_ok is False:
            flags.append(CredibilityFlag(level="amber", check="company_website_down",
                                         message=f"{w.company} is listed ({where}) but its website "
                                                 f"{info.website} didn't respond.", resume=_role(w, today)))
            continue
        bits = [f"founded {info.founded_year}" if info.founded_year else None,
                f"website {info.website} is online" if info.website_ok else None]
        flags.append(CredibilityFlag(level="green", check="company_found",
                                     message=f"{w.company} exists: {where}"
                                             + (f"; {', '.join(b for b in bits if b)}." if any(bits) else "."),
                                     resume=_role(w, today)))
    return flags
