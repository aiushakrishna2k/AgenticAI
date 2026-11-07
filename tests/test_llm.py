import json
from types import SimpleNamespace

import pytest

from expense_tracker.core import llm


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    """Mimics client.beta.messages.stream(...) and records the request kwargs."""

    def __init__(self, payload, stop_reason="end_turn"):
        self.calls = []
        msg = SimpleNamespace(stop_reason=stop_reason,
                              content=[SimpleNamespace(type="text", text=json.dumps(payload))])
        outer = self

        class _Messages:
            def stream(self, **kw):
                outer.calls.append(kw)
                return FakeStream(msg)

        self.beta = SimpleNamespace(messages=_Messages())


EXTRACTED = {
    "bank": "Example Bank", "account_type": "credit", "period_start": "2026-02-26", "period_end": "2026-03-25",
    "opening_balance": 520.0, "closing_balance": 50.99,
    "transactions": [
        {"date": "2026-02-28", "description": "NETFLIX.COM", "amount": 10.99, "direction": "debit", "balance": None},
        {"date": "2026-03-02", "description": "AMAZON  MARKETPLACE", "amount": 29.99, "direction": "debit",
         "balance": None},
        {"date": "2026-03-09", "description": "AMAZON MARKETPLACE REFUND", "amount": 29.99, "direction": "credit",
         "balance": None},
        {"date": "2026-03-22", "description": "PAYMENT RECEIVED", "amount": 500, "direction": "credit",
         "balance": None},
        {"date": "not a date", "description": "junk", "amount": 1, "direction": "debit", "balance": None},
    ],
}


def test_extract_statement_request_and_parsing():
    client = FakeClient(EXTRACTED)
    res = llm.extract_statement(["Card 4929 1234 5678 9012\n28 Feb NETFLIX.COM 10.99"], "cc.pdf", "abc",
                                client=client)
    kw = client.calls[0]
    assert kw["model"] == llm.MODEL
    assert kw["fallbacks"] == "default" and kw["betas"] == [llm.FALLBACK_BETA]
    assert kw["output_config"]["format"]["type"] == "json_schema"
    sent = kw["messages"][0]["content"]
    assert "4929 1234" not in sent and "9012" in sent        # card number masked before sending

    assert res.statement.account_type == "credit"
    assert res.statement.opening_balance_pence == 52000
    assert len(res.transactions) == 4                         # junk row dropped
    assert res.transactions[1].description == "AMAZON MARKETPLACE"
    assert res.transactions[3].amount_pence == 50000


def test_account_type_hint_wins():
    res = llm.build_extraction(EXTRACTED, "x.pdf", "h", account_type_hint="debit")
    assert res.statement.account_type == "debit"
    assert all(t.account_type == "debit" for t in res.transactions)


def test_refusal_and_truncation_raise():
    with pytest.raises(llm.LLMError, match="declined"):
        llm.extract_statement(["x"], "a", "b", client=FakeClient({}, stop_reason="refusal"))
    with pytest.raises(llm.LLMError, match="truncated"):
        llm.extract_statement(["x"], "a", "b", client=FakeClient({}, stop_reason="max_tokens"))


def test_categorize_merchants_maps_ids_and_filters_invalid():
    client = FakeClient({"results": [{"id": 0, "category": "Entertainment"}, {"id": 1, "category": "Bogus"},
                                     {"id": 9, "category": "Travel"}]})
    items = [{"merchant": "ZZYZX ESCAPE", "direction": "debit", "example": "ZZYZX ESCAPE 123"},
             {"merchant": "ACME", "direction": "credit", "example": "ACME"}]
    out = llm.categorize_merchants(items, client=client)
    assert out == {("ZZYZX ESCAPE", "debit"): "Entertainment"}
    assert client.calls[0]["output_config"]["effort"] == "low"


def test_not_configured_without_key():
    assert llm.is_configured() is False
