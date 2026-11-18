"""The manager → researcher feedback loop, with stand-ins for Claude. Made-up names."""
import json
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from ecosystem import models as m
from ecosystem.assistant import card_text, run_assistant
from ecosystem.decisions import summaries
from ecosystem.research import RequestRefused, ask, research_batch
from ecosystem.store import Store

USAGE = NS(input_tokens=2000, output_tokens=300, cache_creation_input_tokens=0, cache_read_input_tokens=0,
           server_tool_use=None)


class Manager:
    """Answers each card with the next queued answer (default: send back once, then link)."""
    def __init__(self, answers):
        self.answers, self.seen = list(answers), []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.seen.append(kw)
        body = self.answers.pop(0)
        return NS(content=[NS(type="text", text=json.dumps(body))], stop_reason="end_turn", usage=USAGE)


class Researcher:
    """Opens the brand's terms page and answers the question from it."""
    def __init__(self):
        self.beta, self.systems, self.briefs, self.models = NS(messages=NS(create=self.create)), [], [], []

    def create(self, **kw):
        self.models.append(kw["model"])
        self.systems.append(kw["system"])
        self.briefs.append(kw["messages"][0]["content"])
        page = NS(type="web_fetch_tool_result", model_dump=lambda: {"type": "web_fetch_tool_result",
                  "content": {"type": "web_fetch_result", "url": "https://www.herencash.example/terms"}})
        rec = NS(type="tool_use", name="record_findings", id="t1", input={
            "summary": "Heron Cash's terms name Heron Holdings, LLC as the lender.", "is_us_consumer_lender": True,
            "lender_category": "state_licensed",
            "findings": [{"attribute": "operator_group", "value": "Heron Holdings, LLC",
                          "source_url": "https://www.herencash.example/terms",
                          "quote": "Loans are made by Heron Holdings, LLC", "confidence": "high"}],
            "answers": [{"question": "Which company is named as the lender in the terms?",
                         "answer": "Heron Holdings, LLC", "source_url": "https://www.herencash.example/terms",
                         "quote": "Loans are made by Heron Holdings, LLC"},
                        {"question": "Is it on a partner list?", "answer": "Yes, see https://made-up.example/list",
                         "source_url": "https://made-up.example/list", "quote": "x"}]})
        return NS(content=[page, rec], stop_reason="tool_use", usage=USAGE)


def _match(store):
    legal, brand = store.new_org("Heron Holdings, LLC"), store.new_org("Heron Cash")
    store.s.flush()
    store.set_our_relationship(brand.id, role="buyer", status="live", source="manual")
    it = store.review("possible_match", f"possible_match:{legal.id}:{brand.id}", "Heron Holdings may be buyer Heron Cash",
                      [legal.id, brand.id], {"reasons": ["spreadsheet 'Matched buyer' says Heron Cash"]})
    store.s.flush()
    return legal, brand, it


def test_full_loop(session):
    store = Store(session, actor="assistant")
    legal, brand, it = _match(store)
    send_back = {"recommendation": "ask_research", "confidence": "medium", "reason": "Need the lender named in the terms.",
                 "questions": ["Which company is named as the lender in the terms?", "Is it on a partner list?"],
                 "investigate": "right", "mode": "ownership"}
    link = {"recommendation": "left_owns_right", "confidence": "high", "reason": "The terms name the LLC as lender.",
            "questions": [], "investigate": "record", "mode": "profile"}
    mgr = Manager([send_back, link])

    out = run_assistant(store, client=mgr)
    assert out["sent_back"] == 1 and it.status == "open"
    req = session.scalar(select(m.ResearchRequest))
    assert req.organisation_id == brand.id and req.mode == "ownership" and req.status == "queued"
    assert it.payload["assistant"]["label"].startswith("Sent back for research")

    # while research is pending the manager waits
    out = run_assistant(store, client=mgr)
    assert out.get("waiting_on_research") == 1 and len(mgr.seen) == 1

    # the research job takes the request first, in ownership mode, with the questions in the brief
    res = Researcher()
    rb = research_batch(Store(session, actor="scheduler"), n=1, client=res)
    assert rb["requests"] == 1 and "ownership and partners investigator" in res.systems[0]
    assert res.models == ["claude-opus-5-5"]  # follow-up questions go to Opus; new companies stay on Sonnet
    assert "Questions to answer:" in res.briefs[0] and "Which company is named" in res.briefs[0]
    session.refresh(req)
    assert req.status == "done" and req.result_item_id
    answer_card = session.get(m.ReviewItem, req.result_item_id)
    assert answer_card.payload["request_for"] == it.id
    cited = [a["cited"] for a in answer_card.payload["answers"]]
    assert cited == [True, False]   # the partner-list URL was never opened, so it isn't trusted

    # the original card now carries the answers, and the manager decides it
    text = card_text(session, it, summaries(session, set(it.entity_ids)))
    assert "FOLLOW-UP RESEARCH 1" in text and "Heron Holdings, LLC" in text and "no verified source" in text
    run_assistant(store, client=mgr)
    assert it.payload["assistant"]["action"] == f"link:{legal.id}:{brand.id}"


def test_limits(session):
    store = Store(session, actor="t")
    legal, brand, it = _match(store)
    with pytest.raises(RequestRefused):
        ask(store, brand.id, ["   "], review_item=it)
    r1 = ask(store, brand.id, ["a", "b", "c", "d"], review_item=it)
    assert len(r1.questions) == 3
    with pytest.raises(RequestRefused):  # one open request per card
        ask(store, brand.id, ["again"], review_item=it)
    r1.status = "done"
    ask(store, brand.id, ["second"], review_item=it).status = "done"
    with pytest.raises(RequestRefused):  # sent back twice already: a person decides
        ask(store, brand.id, ["third"], review_item=it)


def test_manager_cannot_send_back_past_the_limit(session):
    store = Store(session, actor="assistant")
    legal, brand, it = _match(store)
    for q in ("one", "two"):
        ask(store, brand.id, [q], review_item=it).status = "done"
    mgr = Manager([{"recommendation": "ask_research", "confidence": "low", "reason": "x", "questions": ["three"],
                    "investigate": "left", "mode": "ownership"}])
    run_assistant(store, client=mgr)
    assert it.payload["assistant"]["choice"] == "unsure"  # not an allowed choice any more


def test_related_keeps_it_held_back_and_closes_the_card(session):
    from ecosystem.decisions import decide
    from ecosystem.web.views import org_table
    store = Store(session, actor="t")
    legal, brand, it = _match(store)
    sister = store.new_org("Heron Sister Lending")
    session.flush()
    it2 = store.review("possible_match", "pm:sister", "Sister may be buyer Heron Cash", [sister.id, brand.id], {"reasons": []})
    decide(store, it2, "related", "same tribe, separate brand")
    assert it2.status == "approved"
    claim = session.scalar(select(m.Claim).where(m.Claim.entity_id == sister.id, m.Claim.attribute == "partner_related"))
    assert claim.value["partner"] == "Heron Cash"
    assert session.scalar(select(m.Claim).where(m.Claim.entity_id == brand.id, m.Claim.attribute == "partner_related")) is None


def test_sentence_owner_text_never_becomes_a_company(session):
    from ecosystem import parsing as p
    from ecosystem.research import cleanup_sentence_orgs
    assert p.group_name("ECN Capital Corp acquired a majority stake in August 2024") is None
    assert p.group_name("Iipay Nation of Santa Ysabel; sister brands include X") == "Iipay Nation of Santa Ysabel"
    assert p.group_name("Owned by Acme Holdings, Inc.") == "Acme Holdings, Inc."
    store = Store(session, actor="upkeep")
    junk = store.new_org("Acquired by an investor group led by somebody in 2023", kind="operator_group")
    named = store.new_org("Pomo Gold, an entity of the Elem Indian Colony", kind="operator_group")
    child = store.new_org("Child Lending")
    session.flush()
    child.parent_id = junk.id
    it = store.review("possible_match", "pm:junk", "junk", [junk.id, child.id], {"reasons": []})
    out = cleanup_sentence_orgs(store)
    assert out == {"renamed": 1, "duplicates": 0, "screened_out": 1}
    assert junk.lifecycle == "screened_out" and child.parent_id is None and it.status == "rejected"
    assert named.display_name == "Pomo Gold"
