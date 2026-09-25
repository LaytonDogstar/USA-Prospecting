"""Screening and scoring tests on synthetic prospect data (no network)."""
import json
import tempfile
import unittest
from pathlib import Path

from prospecting import db
from prospecting.discover import SOURCE, screen
from prospecting.research import load_prospect_research
from prospecting.scoring import _max_loan, score


class ScreenTest(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(screen("Example Bank, N.A."), "bank or credit union")
        self.assertEqual(screen("Somebody Credit Union"), "bank or credit union")
        self.assertEqual(screen("PatientFinance Co"), "healthcare / retail point-of-sale finance")
        self.assertIsNone(screen("Example Lending LLC"))
        self.assertIsNone(screen("Tribal Economic Development Corporation"))

    def test_max_loan(self):
        self.assertEqual(_max_loan("$200 - $5,000"), 5000)
        self.assertEqual(_max_loan("up to $2.5k"), 2500)
        self.assertIsNone(_max_loan(None))


class ScoringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.conn = db.connect(self.tmp / "t.db")
        self.conn.execute("INSERT INTO buyer (canonical_name, category) VALUES ('Our Client', 'D')")
        for name, n, buyer in [("Operator Group LLC", 100, None), ("Small State Lender", 10, None),
                               ("Prime Lender Inc", 200, None), ("Client Holdco", 300, 1)]:
            cid = self.conn.execute("INSERT INTO company (name, buyer_id, source) VALUES (?,?,?)",
                                    (name, buyer, SOURCE)).lastrowid
            self.conn.execute("INSERT INTO company_fact (company_id, field, value, source_url) VALUES (?,?,?,?)",
                              (cid, "complaints_total", str(n), "x"))
        research = self.tmp / "p.json"
        research.write_text(json.dumps([
            {"buyer": "Operator Group LLC", "website": "op.example", "confidence": "high", "facts": [
                {"field": "company_type", "value": "servicer/platform", "source_url": "https://op.example/a"},
                {"field": "consumer_brands", "value": "Brand A (a.example)", "source_url": "https://op.example/b"},
                {"field": "consumer_brands", "value": "Brand B (b.example)", "source_url": "https://op.example/b"},
                {"field": "loan_amount_range", "value": "$300 - $5,000", "source_url": "https://op.example/c"},
                {"field": "storefront_or_online", "value": "online", "source_url": "https://op.example/c"},
                {"field": "lead_buying_signals", "value": "affiliate program page", "source_url": "https://op.example/d"},
                {"field": "customer_segment", "value": "subprime", "source_url": "https://op.example/c"}]},
            {"buyer": "Small State Lender", "confidence": "medium", "facts": [
                {"field": "company_type", "value": "state-licensed", "source_url": "https://s.example"},
                {"field": "loan_amount_range", "value": "$100 - $1,000", "source_url": "https://s.example"},
                {"field": "products", "value": "payday loan", "source_url": "https://s.example"}]},
            {"buyer": "Prime Lender Inc", "confidence": "high", "facts": [
                {"field": "company_type", "value": "fintech-other", "source_url": "https://p.example"},
                {"field": "customer_segment", "value": "prime", "source_url": "https://p.example"}]},
            {"buyer": "Unknown Name", "confidence": "low", "facts": []},
        ]))
        self.res = load_prospect_research(self.conn, [research], SOURCE)

    def test_load(self):
        self.assertEqual(self.res["companies"], 3)
        self.assertEqual(self.res["unmatched"], ["Unknown Name"])

    def test_ranking(self):
        rows = {r["company"]: r for r in score(self.conn)}
        self.assertNotIn("Client Holdco", rows)                     # existing clients never scored
        op, small, prime = rows["Operator Group LLC"], rows["Small State Lender"], rows["Prime Lender Inc"]
        self.assertGreater(op["score"], small["score"])
        self.assertGreater(small["score"], prime["score"])          # prime penalty outweighs size
        self.assertEqual(op["fit"], "price")
        self.assertEqual(small["fit"], "volume")
        self.assertEqual(op["type_key"], "operator")
        self.assertIn("2 brand(s)", op["why"])


if __name__ == "__main__":
    unittest.main()
