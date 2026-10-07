from uuid import uuid4

import httpx

from nexus.backend.agent.market_data import (
    parse_macro_request, parse_sec_request, fetch_macro_indicator, fetch_sec_filings,
)


def test_external_market_request_parsers_are_bounded():
    assert parse_macro_request("中国最新 GDP 增速是多少") == "gdp_growth"
    assert parse_macro_request("国内 CPI 通胀情况") == "inflation"
    assert parse_macro_request("美国 GDP") is None
    assert parse_sec_request("苹果最新 SEC 财报") == "AAPL"
    assert parse_sec_request("NVDA 最新 10-Q") == "NVDA"
    assert parse_sec_request("某公司财报") is None


async def test_world_bank_response_is_validated_and_structured():
    async def handler(request):
        assert request.url.host == "api.worldbank.org"
        assert "NY.GDP.MKTP.KD.ZG" in request.url.path
        return httpx.Response(200, json=[{"lastupdated":"2026-07-13"}, [
            {"date":"2025", "value":4.95994886240992}
        ]])
    async with httpx.AsyncClient(base_url="https://api.worldbank.org", transport=httpx.MockTransport(handler)) as client:
        answer = await fetch_macro_indicator("gdp_growth", client)
    assert answer["type"] == "external_macro"
    assert answer["value"] == "4.96%" and answer["period"] == "2025"
    assert answer["cache"] == "live"


async def test_sec_response_only_exposes_supported_filings():
    async def handler(request):
        assert request.url.host == "data.sec.gov"
        assert request.headers["User-Agent"].startswith("Nexus")
        return httpx.Response(200, json={
            "name":"Apple Inc.",
            "filings":{"recent":{
                "form":["4","10-Q","8-K"],
                "filingDate":["2026-09-01","2026-08-01","2026-07-15"],
                "reportDate":["2026-09-01","2026-06-30","2026-07-15"],
                "accessionNumber":["0001-00-001","0001-00-002","0001-00-003"],
                "primaryDocument":["x4.htm","q.htm","k.htm"],
            }}
        })
    async with httpx.AsyncClient(base_url="https://data.sec.gov", transport=httpx.MockTransport(handler)) as client:
        answer = await fetch_sec_filings("AAPL", client)
    assert answer["type"] == "sec_filings"
    assert [row["form"] for row in answer["filings"]] == ["10-Q", "8-K"]
    assert all(row["url"].startswith("https://www.sec.gov/Archives/edgar/data/320193/") for row in answer["filings"])


async def test_macro_request_routes_without_model(client, monkeypatch):
    from nexus.backend.agent import conversation
    async def fake_fetch(indicator):
        return {"type":"external_macro", "title":"中国 GDP 实际增速", "value":"4.96%", "period":"2025", "trace":[]}
    monkeypatch.setattr(conversation, "fetch_macro_indicator", fake_fetch)
    response = await client.post('/api/messages', json={'message':'中国最新 GDP 增速是多少','request_id':str(uuid4())})
    assert response.status_code == 200
    assert response.json()["type"] == "external_macro"
