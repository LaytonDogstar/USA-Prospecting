from ecosystem import parsing as p
from ecosystem.normalise import compact, is_generic, name_similarity, norm_domain, norm_name, split_dba


def test_norm_name_and_compact():
    assert norm_name("Acme Lending, LLC") == "acme lending"
    assert norm_name("The Example Company, Inc.") == "example"
    assert compact("Acme Loans") == compact("AcmeLoans") == "acmeloans"
    assert split_dba("Foo Lending LLC dba Bar Cash") == ("Foo Lending LLC", ["Bar Cash"])
    assert split_dba("Foo, Inc. d/b/a BarLend") == ("Foo, Inc.", ["BarLend"])


def test_domains():
    assert norm_domain("https://www.Example.com/path?x=1") == "example.com"
    assert norm_domain("—") is None
    assert norm_domain("not a domain") is None


def test_generic_names_do_not_match():
    assert is_generic("Credit Corp") and is_generic("lending") and not is_generic("Zephyr Loans")
    assert name_similarity("Zephyr Loans", "Zephyr Loans LLC") == 1.0
    assert name_similarity("credit", "Zephyr Credit") < 0.9


def test_lender_roles():
    assert p.lender_roles("tribal") == ["lender_tribal"]
    assert p.lender_roles("state-licensed; CAB/CSO in Texas") == ["lender_state_licensed", "lender_cab_cso"]
    assert p.lender_roles("state-licensed plus bank-partner (First Example Bank)") == ["lender_state_licensed", "lender_bank_partner"]
    assert p.lender_roles("fintech-other") == []


def test_loan_range():
    assert p.loan_range("$500 - $5,000") == (500, 5000)
    assert p.loan_range("up to $1,500 for first-time customers; up to $5,000 for returning") == (None, 5000)
    assert p.loan_range("A: up to $1,200 new / $3,000 VIP; B: up to $1,000 new / $2,500 VIP") == (None, 3000)
    assert p.loan_range("$300 - $2,500 (up to $1,200 new on homepage)") == (300, 2500)
    assert p.loan_range("Installment loans typically $100–$4,000") == (100, 4000)
    assert p.loan_range("up to $2,500") == (None, 2500)
    assert p.loan_range(None) == (None, None)


def test_states():
    r = p.states("Not available in AR, CT, DC, Georgia")
    assert r["basis"] == "excluded" and r["states"] == ["GA", "AR", "CT", "DC"]
    r = p.states("Tennessee, Kentucky, West Virginia")
    assert r["basis"] == "lends" and r["states"] == ["TN", "KY", "WV"]
    # negations the first parser read backwards
    r = p.states("Not all states; does not lend in Alaska")
    assert r["basis"] == "excluded" and r["states"] == ["AK"]
    r = p.states("Most states; not CO, CT, ME, NV")
    assert r["basis"] == "excluded" and r["states"] == ["CO", "CT", "ME", "NV"]
    r = p.states("Federal-employee program in all 50 states, DC and PR except WV")
    assert r["basis"] == "excluded" and r["states"] == ["WV"] and not r["nationwide"]
    r = p.states("40 states (as of Dec 31, 2025)")
    assert r["count"] == 40 and r["basis"] is None
    assert p.states("Complaint states: CA (5), OR, KS")["states"] == []
    assert p.states("nationwide (via a bank partnership)")["nationwide"]


def test_brands():
    b = p.brands("Alpha Loans (alphaloans.com) - Alpha Finance, LLC; Beta Cash - betacash.com; Gamma")
    assert [x["name"] for x in b] == ["Alpha Loans", "Beta Cash", "Gamma"]
    assert b[0]["domains"] == ["alphaloans.com"] and b[0]["entity"] == "Alpha Finance, LLC"
    assert b[1]["domains"] == ["betacash.com"] and b[1]["entity"] is None
    b = p.brands("DeltaUSA (deltausa.com), operated by Delta Online Holdings, LLC; DeltaCredit (deltacredit.com)")
    assert b[0]["entity"] == "Delta Online Holdings, LLC" and b[1]["name"] == "DeltaCredit"


def test_list_appearances_and_flags_and_sources():
    assert p.list_appearances("alpha.com (+5 sites), beta.com") == [
        {"domain": "alpha.com", "family_size": 6}, {"domain": "beta.com", "family_size": 1}]
    assert p.compliance_flags("Washington DFI consumer alert: not licensed in WA") == ["state_licence_alert"]
    assert p.compliance_flags("Lawsuits allege rent-a-tribe schemes") == ["rent_a_tribe_litigation"]
    assert p.compliance_flags("Note FTC 2022 action") == ["ftc_action"]
    assert p.source_type_for_url("https://www.sec.gov/x") == "sec_filing"
    assert p.source_type_for_url("https://dfi.wa.gov/alert") == "regulator"
    assert p.hedge_status("POSSIBLE Example Group - check") == "rumoured"
    assert p.hedge_status("Example Finance (inferred)") == "inferred"
    assert p.strip_hedge("POSSIBLE Example Group (your note) - check") == "Example Group"


def test_traffic_role():
    assert p.traffic_role("3 Comparison / offer-wall platform") == "comparison_offer_wall"
    assert p.traffic_role(None) is None
