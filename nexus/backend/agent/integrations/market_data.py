"""Official, allowlisted market context tools with bounded responses and local cache."""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from ...core.exceptions import BusinessRuleException
from ..security import untrusted


WORLD_BANK_ORIGIN = "https://api.worldbank.org"
WORLD_BANK_SOURCE = "https://data.worldbank.org/country/china"
SEC_ORIGIN = "https://data.sec.gov"
SEC_SOURCE = "https://www.sec.gov/edgar/search/"

MACRO_INDICATORS = {
    "inflation": {"code": "FP.CPI.TOTL.ZG", "label": "中国居民消费价格通胀率", "unit": "%", "patterns": ("通胀", "CPI", "物价涨幅")},
    "gdp_growth": {"code": "NY.GDP.MKTP.KD.ZG", "label": "中国 GDP 实际增速", "unit": "%", "patterns": ("GDP", "经济增速", "国内生产总值")},
    "unemployment": {"code": "SL.UEM.TOTL.ZS", "label": "中国失业率（ILO 估算）", "unit": "%", "patterns": ("失业率", "就业率")},
}

SEC_COMPANIES = {
    "AAPL": (320193, "Apple Inc.", ("苹果", "APPLE")),
    "MSFT": (789019, "Microsoft Corporation", ("微软", "MICROSOFT")),
    "NVDA": (1045810, "NVIDIA Corporation", ("英伟达", "NVIDIA")),
    "TSLA": (1318605, "Tesla, Inc.", ("特斯拉", "TESLA")),
    "AMZN": (1018724, "Amazon.com, Inc.", ("亚马逊", "AMAZON")),
    "GOOGL": (1652044, "Alphabet Inc.", ("谷歌", "ALPHABET", "GOOGLE")),
    "META": (1326801, "Meta Platforms, Inc.", ("META", "脸书", "FACEBOOK")),
}

_CACHE: dict[str, tuple[float, dict]] = {}


def parse_macro_request(text: str) -> str | None:
    value = text.upper()
    if not any(word in value for word in ("中国", "国内", "宏观")):
        return None
    for key, item in MACRO_INDICATORS.items():
        if any(pattern.upper() in value for pattern in item["patterns"]):
            return key
    return None


def parse_sec_request(text: str) -> str | None:
    value = text.upper().strip()
    if not any(word in value for word in ("财报", "公告", "披露", "SEC", "10-K", "10-Q", "8-K")):
        return None
    for ticker, (_, _, aliases) in SEC_COMPANIES.items():
        if re.search(rf"(?<![A-Z]){re.escape(ticker)}(?![A-Z])", value) or any(alias.upper() in value for alias in aliases):
            return ticker
    return None


def _cached(key: str, ttl: int) -> dict | None:
    row = _CACHE.get(key)
    if row and time.monotonic() - row[0] <= ttl:
        return {**row[1], "cache": "fresh"}
    return None


def _stale(key: str) -> dict | None:
    row = _CACHE.get(key)
    if not row:
        return None
    stale = {**row[1], "cache": "stale"}
    stale.pop("notice", None)
    return stale


def _store(key: str, answer: dict) -> dict:
    _CACHE[key] = (time.monotonic(), answer)
    return {**answer, "cache": "live"}


async def fetch_macro_indicator(indicator: str, client: httpx.AsyncClient | None = None) -> dict:
    if indicator not in MACRO_INDICATORS:
        raise BusinessRuleException("暂不支持这个宏观指标")
    cache_key = f"wb:{indicator}"
    if hit := _cached(cache_key, 24 * 60 * 60):
        return hit
    item = MACRO_INDICATORS[indicator]

    async def request(active: httpx.AsyncClient) -> dict:
        response = await active.get(f"/v2/country/CN/indicator/{item['code']}", params={"format": "json", "per_page": "8"}, headers={"Accept": "application/json"})
        if response.status_code != 200 or len(response.content) > 128_000:
            raise BusinessRuleException("世界银行数据服务暂时不可用")
        try:
            payload = response.json()
            metadata, records = payload
            record = next(row for row in records if row.get("value") is not None)
            value = Decimal(str(record["value"])).quantize(Decimal("0.01"))
            year = str(record["date"])
            updated = str(metadata.get("lastupdated") or year)
        except (ValueError, TypeError, KeyError, StopIteration, ArithmeticError):
            raise BusinessRuleException("世界银行数据格式异常") from None
        answer = {
            "type": "external_macro", "title": item["label"], "value": f"{value:,.2f}{item['unit']}",
            "period": year, "updated": updated, "indicator_code": item["code"],
            "source": {"name": "世界银行 Indicators API", "url": WORLD_BANK_SOURCE},
            "engine": "external-tool",
            "trace": [
                {"label": "识别指标", "detail": f"中国 · {item['code']}", "status": "done"},
                {"label": "调用官方数据", "detail": "World Bank V2 Indicators API", "status": "done"},
                {"label": "校验与缓存", "detail": "选择最近非空值 · 本地缓存 24 小时", "status": "done"},
            ],
        }
        return _store(cache_key, answer)

    try:
        if client is not None:
            return await request(client)
        async with httpx.AsyncClient(base_url=WORLD_BANK_ORIGIN, timeout=8.0, follow_redirects=False, trust_env=False) as active:
            return await request(active)
    except BusinessRuleException:
        if cached := _stale(cache_key):
            return cached
        raise
    except (httpx.HTTPError, TimeoutError):
        if cached := _stale(cache_key):
            return cached
        raise BusinessRuleException("世界银行数据服务暂时不可用") from None


async def fetch_sec_filings(ticker: str, client: httpx.AsyncClient | None = None) -> dict:
    ticker = ticker.upper()
    if ticker not in SEC_COMPANIES:
        raise BusinessRuleException("当前仅支持页面列出的美股公司代码")
    cache_key = f"sec:{ticker}"
    if hit := _cached(cache_key, 15 * 60):
        return hit
    cik, fallback_name, _ = SEC_COMPANIES[ticker]

    async def request(active: httpx.AsyncClient) -> dict:
        response = await active.get(f"/submissions/CIK{cik:010d}.json", headers={"Accept": "application/json", "User-Agent": "Nexus local financial agent demo nexus@example.invalid"})
        if response.status_code != 200 or len(response.content) > 2_000_000:
            raise BusinessRuleException("SEC EDGAR 数据服务暂时不可用")
        try:
            payload = response.json()
            recent = payload["filings"]["recent"]
            rows = []
            for index, form in enumerate(recent["form"]):
                if form not in {"10-K", "10-Q", "8-K", "20-F", "6-K"}:
                    continue
                accession = str(recent["accessionNumber"][index])
                document = str(recent["primaryDocument"][index])
                rows.append({
                    "form": form, "filed": str(recent["filingDate"][index]),
                    "report_date": str(recent["reportDate"][index]), "accession": accession,
                    "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{document}",
                })
                if len(rows) == 6:
                    break
            if not rows:
                raise KeyError("no supported filings")
            # "name" is free text the company itself filed with the SEC. It is
            # rendered to the customer, so it has to clear the same boundary as
            # any other third-party string before it reaches our markup.
            name = untrusted.clean(payload.get("name"), fallback_name, field="SEC 公司名")
        except (ValueError, TypeError, KeyError, IndexError):
            raise BusinessRuleException("SEC EDGAR 数据格式异常") from None
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        answer = {
            "type": "sec_filings", "title": f"{name} · 最新官方披露", "ticker": ticker,
            "company": name, "filings": rows, "checked_at": checked_at,
            "source": {"name": "SEC EDGAR", "url": SEC_SOURCE},
            "engine": "external-tool",
            "trace": [
                {"label": "识别公司", "detail": f"{ticker} · CIK {cik:010d}", "status": "done"},
                {"label": "调用官方数据", "detail": "SEC submissions API · 无需 API Key", "status": "done"},
                {"label": "结构化筛选", "detail": "仅保留 10-K / 10-Q / 8-K / 20-F / 6-K", "status": "done"},
            ],
        }
        return _store(cache_key, answer)

    try:
        if client is not None:
            return await request(client)
        async with httpx.AsyncClient(base_url=SEC_ORIGIN, timeout=8.0, follow_redirects=False, trust_env=False) as active:
            return await request(active)
    except BusinessRuleException:
        if cached := _stale(cache_key):
            return cached
        raise
    except (httpx.HTTPError, TimeoutError):
        if cached := _stale(cache_key):
            return cached
        raise BusinessRuleException("SEC EDGAR 数据服务暂时不可用") from None
