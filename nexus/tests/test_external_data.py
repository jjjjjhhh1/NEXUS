from decimal import Decimal
from uuid import uuid4

import httpx

from nexus.backend.agent.external_data import parse_fx_request, fetch_fx_quote, capability_catalog


def test_fx_parser_supports_lookup_and_local_conversion():
    assert parse_fx_request("人民币兑美元汇率") == (Decimal("1"), "CNY", "USD")
    assert parse_fx_request("1000人民币能换多少美元") == (Decimal("1000"), "CNY", "USD")
    assert parse_fx_request("今天天气怎么样") is None


async def test_fx_provider_response_is_validated_and_amount_stays_local():
    seen = []

    def handler(request: httpx.Request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"date": "2026-09-25", "base": "CNY", "quote": "USD", "rate": 0.14})

    async with httpx.AsyncClient(base_url="https://api.frankfurter.dev", transport=httpx.MockTransport(handler)) as client:
        result = await fetch_fx_quote(Decimal("1000"), "CNY", "USD", client)
    assert result["type"] == "external_data"
    assert result["summary"] == "1,000.00 CNY ≈ 140.00 USD"
    assert seen == ["https://api.frankfurter.dev/v2/rate/cny/usd"]
    assert "1000" not in seen[0]
    assert len(result["trace"]) == 3


def test_capability_catalog_is_honest_about_boundaries():
    catalog = capability_catalog(True)
    assert len(catalog["capabilities"]) == 16
    assert any(item["id"] == "fx" and item["status"] == "available" for item in catalog["capabilities"])
    assert "不连接真实银行账户" in catalog["limitations"]


async def test_fx_message_uses_external_tool(client, monkeypatch):
    from nexus.backend.agent import conversation

    async def fake(amount, base, quote):
        return {
            "type": "external_data", "title": "CNY / USD 参考汇率",
            "summary": "1,000.00 CNY ≈ 140.00 USD", "rate": "0.14",
            "amount": "1,000.00", "converted": "140.00", "base": base, "quote": quote,
            "as_of": "2026-09-25", "source": {"name": "Frankfurter 汇率 API", "url": "https://frankfurter.dev/"},
            "notice": "参考汇率", "engine": "external-tool", "trace": [],
        }

    monkeypatch.setattr(conversation, "fetch_fx_quote", fake)
    response = await client.post("/api/messages", json={"message": "1000人民币能换多少美元", "request_id": str(uuid4())})
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "external_data" and body["converted"] == "140.00"
