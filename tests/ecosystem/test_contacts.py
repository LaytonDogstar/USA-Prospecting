"""Contacts job with a stand-in for Hunter. Made-up names and domains only."""
from types import SimpleNamespace as NS

from sqlalchemy import select

from ecosystem import models as m
from ecosystem.contacts import find_contacts, pick, rank_title, set_do_not_contact
from ecosystem.research import add_researched_person


class FakeHunter:
    def __init__(self, emails=None, finder=None):
        self.emails, self.finder = emails if emails is not None else {}, finder or {}
        self.calls = []

    def domain_search(self, domain, limit=10):
        self.calls.append(("search", domain))
        return {"emails": self.emails.get(domain, [])}

    def verify(self, email):
        self.calls.append(("verify", email))
        return {"status": "valid", "score": 91}

    def email_finder(self, domain, first, last):
        self.calls.append(("finder", domain, first, last))
        return self.finder.get((first, last), {})


def _email(addr, first, last, position, status="valid", conf=90, kind="personal", seniority=None):
    return {"value": addr, "type": kind, "first_name": first, "last_name": last, "position": position,
            "seniority": seniority, "confidence": conf,
            "verification": {"status": status} if status else {},
            "sources": [{"uri": "https://www.wrenloans.example/team"}]}


def _prospect(store, name="Wren Loans", domain="wrenloans.example", score=72):
    org = store.new_org(name)
    store.s.flush()
    store.add_domain(org.id, domain, primary=True)
    store.s.add(m.Score(entity_id=org.id, score_type="buyer_value", value=score, components={}))
    store.s.flush()
    return org


def test_rank_and_pick_drop_generic_and_irrelevant():
    assert rank_title("Director of Affiliate Partnerships")[1] == "affiliate_partnerships"
    assert rank_title("Software Engineer")[0] == 0
    assert rank_title("Vice President of Technology", "executive")[0] == 0
    assert rank_title("UK Managing Director")[0] == 0
    assert rank_title("Director of Operations", "executive")[0] == 0
    assert pick([_email("wren@wrenloans.example", "Wren", "Ops", "Marketing Director")]) == []
    got = pick([
        _email("info@wrenloans.example", "Info", "Desk", "Marketing"),
        _email("ann.vole@wrenloans.example", "Ann", "Vole", "Software Engineer"),
        _email("bo.teal@wrenloans.example", "Bo", "Teal", "VP Marketing"),
        _email("cy.lark@wrenloans.example", "Cy", "Lark", "Head of Partnerships"),
        _email("di.moth@wrenloans.example", "Di", "Moth", "CEO", seniority="executive"),
        _email("ed.newt@wrenloans.example", "Ed", "Newt", "Lead Acquisition Manager", status="invalid"),
        _email("fi.wasp@wrenloans.example", "Fi", "Wasp", "Growth Lead"),
    ])
    assert [e["_email"] for e in got] == ["cy.lark@wrenloans.example", "di.moth@wrenloans.example",
                                          "bo.teal@wrenloans.example"]
    assert all(not e["_email"].startswith(("info@", "ann.", "ed.")) for e in got)


def test_contacts_saves_best_three_with_sources(store):
    org = _prospect(store)
    hunter = FakeHunter({"wrenloans.example": [
        _email("cy.lark@wrenloans.example", "Cy", "Lark", "Head of Partnerships", status=None),
        _email("bo.teal@wrenloans.example", "Bo", "Teal", "VP Marketing"),
        _email("ann.vole@wrenloans.example", "Ann", "Vole", "Engineer"),
    ]})
    out = find_contacts(store, client=hunter)
    assert out["looked_up"] == 1 and out["people"] == 2
    people = list(store.s.scalars(select(m.Person).where(m.Person.organisation_id == org.id)))
    assert {p.email for p in people} == {"cy.lark@wrenloans.example", "bo.teal@wrenloans.example"}
    assert ("verify", "cy.lark@wrenloans.example") in hunter.calls  # no status from the search → verified
    assert all(p.email_status == "valid" and "wrenloans.example/team" in p.source for p in people)
    # a second run leaves it alone: it has people now
    assert find_contacts(store, client=FakeHunter())["looked_up"] == 0


def test_contacts_skips_partners_held_back_and_do_not_contact(store):
    partner = _prospect(store, "Pike Credit", "pikecredit.example")
    store.set_our_relationship(partner.id, role="buyer", status="live", source="manual")
    held = _prospect(store, "Pike Credit Holdings", "pikeholdings.example")
    store.review("possible_match", f"possible_match:{held.id}:{partner.id}", "x", [held.id, partner.id], {})
    dnc = _prospect(store, "Stoat Lending", "stoat.example")
    set_do_not_contact(store, dnc, True, "asked not to be contacted")
    low = _prospect(store, "Gnat Loans", "gnat.example", score=40)
    hunter = FakeHunter()
    out = find_contacts(store, client=hunter)
    assert out["looked_up"] == 0 and hunter.calls == []
    # the button still refuses a possible partner
    assert find_contacts(store, org_ids=[held.id], client=hunter)["skipped"] == [held.id]
    # but looks up a low scorer on request
    assert find_contacts(store, org_ids=[low.id], client=hunter)["looked_up"] == 1


def test_nobody_found_goes_to_researchers_then_email_finder(store):
    org = _prospect(store)
    out = find_contacts(store, client=FakeHunter())
    assert out["sent_to_research"] == 1
    req = store.s.scalar(select(m.ResearchRequest).where(m.ResearchRequest.organisation_id == org.id))
    assert req.mode == "people" and req.status == "queued"
    # not asked twice, and not looked up again within 90 days
    assert find_contacts(store, client=FakeHunter())["looked_up"] == 0
    # research names a person; approving the card creates them without an email
    src = store.url_source("https://www.wrenloans.example/about", "site_read")
    person = add_researched_person(store, org, "Mara Finch | Chief Marketing Officer", src)
    assert person.role_category == "growth_cmo" and person.email is None
    assert add_researched_person(store, org, "Mara Finch | CMO", src) is None  # already on record
    hunter = FakeHunter(finder={("Mara", "Finch"): {"email": "mara@wrenloans.example", "score": 88,
                                                     "verification": {"status": "valid"}}})
    out = find_contacts(store, client=hunter)
    assert out["emails_found"] == 1
    store.s.refresh(person)
    assert person.email == "mara@wrenloans.example" and person.email_status == "valid"


def test_people_mode_research_uses_people_prompt(store):
    from ecosystem.research import ask, research_org
    org = _prospect(store)
    req = ask(store, org.id, ["Who leads partnerships?"], mode="people")
    seen = {}

    def create(**kw):
        seen.update(kw)
        rec = NS(type="tool_use", name="record_findings", id="t", input={
            "summary": "s", "is_us_consumer_lender": True, "lender_category": "unclear", "findings": [], "answers": []})
        return NS(content=[rec], stop_reason="tool_use",
                  usage=NS(input_tokens=10, output_tokens=10, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0, server_tool_use=None))
    research_org(store, org, NS(beta=NS(messages=NS(create=create))), request=req)
    assert "decision-makers" in seen["system"] and "LinkedIn" in seen["system"]
    enum = seen["tools"][-1]["input_schema"]["properties"]["findings"]["items"]["properties"]["attribute"]["enum"]
    assert enum == ["person"]


def test_person_with_email_claim_can_be_deleted(store):
    from ecosystem.web.routes.entity import delete_person
    org = _prospect(store)
    find_contacts(store, client=FakeHunter({"wrenloans.example": [
        _email("bo.teal@wrenloans.example", "Bo", "Teal", "VP Marketing")]}))
    person = store.s.scalar(select(m.Person).where(m.Person.organisation_id == org.id))
    req = NS(session={})
    delete_person(person.id, req, reason="deletion request", db=store.s, user=NS(username="tester", role="admin"))
    assert store.s.get(m.Person, person.id) is None
    assert not store.s.scalars(select(m.Claim).where(m.Claim.entity_id == person.id)).all()


def test_people_research_keeps_relevant_titles_only(store):
    org = _prospect(store)
    src = store.url_source("https://www.wrenloans.example/leadership", "site_read")
    assert add_researched_person(store, org, "Pia Rook | Chief Financial Officer", src) is None
    assert add_researched_person(store, org, "Lou Hart | Chief Operating Officer", src).role_category == "ceo_gm"


def test_card_with_cited_answers_is_not_safely_rejected(store):
    from ecosystem.decisions import suggest
    org = _prospect(store)
    it = store.review("research", f"research:{org.id}:1:req1", "Follow-up research", [org.id],
                      {"run": 1, "counts": {}, "answers": [{"question": "q", "answer": "Lou Hart, COO", "cited": True}]})
    s = suggest(store, it, {})
    assert s["action"] == "reject" and s["safe"] is False
