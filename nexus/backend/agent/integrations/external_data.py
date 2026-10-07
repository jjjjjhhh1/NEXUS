"""Allowlisted, read-only external data tools for the local Agent demo."""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP

import httpx

from ...core.exceptions import BusinessRuleException
from ..security import untrusted


FX_API_ORIGIN = "https://api.frankfurter.dev"
FX_SOURCE_PAGE = "https://frankfurter.dev/"
SUPPORTED_CURRENCIES = {
    "人民币": "CNY", "元": "CNY", "CNY": "CNY",
    "美元": "USD", "美金": "USD", "USD": "USD",
    "欧元": "EUR", "EUR": "EUR",
    "英镑": "GBP", "GBP": "GBP",
    "日元": "JPY", "JPY": "JPY",
    "港币": "HKD", "港元": "HKD", "HKD": "HKD",
    "澳元": "AUD", "AUD": "AUD",
    "加元": "CAD", "CAD": "CAD",
    "新加坡元": "SGD", "SGD": "SGD",
}


def parse_fx_request(text: str, strict: bool = True) -> tuple[Decimal, str, str] | None:
    """Parse explicit FX lookup/conversion requests without sending prose to a provider.

    ``strict`` requires one of the conventional conversion phrasings before it
    will look for a pair. That guard is what keeps "我同时有美元和欧元" from
    being read as a request to convert, so it stays on for routing.

    It is turned off only when the caller has already established intent some
    other way — the model selected the rate tool, so the remaining question is
    only which pair was meant. Re-demanding a magic phrase from a customer who
    already asked a clear question would be the parser second-guessing the
    understanding layer.
    """
    value = text.strip().upper().replace("，", "").replace(",", "")
    if strict and not any(word in value for word in ("汇率", "兑换", "换成", "能换", "折合", "兑")):
        return None
    found: list[tuple[int, int, str]] = []
    for alias, code in sorted(SUPPORTED_CURRENCIES.items(), key=lambda item: len(item[0]), reverse=True):
        for match in re.finditer(re.escape(alias), value, re.I):
            if not any(start <= match.start() < end for start, end, _ in found):
                found.append((match.start(), match.end(), code))
    found.sort()
    codes = []
    for _, _, code in found:
        if not codes or codes[-1] != code:
            codes.append(code)
    if len(codes) == 1 and not strict:
        # "我账户里够换 1000 美元吗" names only the target. The source is not
        # missing information, it is the currency the account is held in, so it
        # is known by construction rather than by being spoken. In strict mode
        # the pair still has to be stated, because that is what keeps an
        # incidental mention from being read as a request to convert.
        if codes[0] != "CNY":
            codes.insert(0, "CNY")
    if len(codes) < 2:
        return None
    base, quote = codes[0], codes[1]
    if base == quote:
        raise BusinessRuleException("请输入两个不同币种，例如人民币兑美元")
    amount_match = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)\s*(?:人民币|元|CNY|美元|美金|USD|欧元|EUR|英镑|GBP|日元|JPY|港币|港元|HKD|澳元|AUD|加元|CAD|新加坡元|SGD)", value, re.I)
    amount = Decimal(amount_match.group(1)) if amount_match else Decimal("1")
    if amount <= 0 or amount > Decimal("1000000000"):
        raise BusinessRuleException("换算金额必须大于 0 且不超过 10 亿元")
    return amount, base, quote


async def fetch_fx_quote(amount: Decimal, base: str, quote: str, client: httpx.AsyncClient | None = None) -> dict:
    """Fetch a reference rate. Only the currency pair leaves this application."""
    if base not in set(SUPPORTED_CURRENCIES.values()) or quote not in set(SUPPORTED_CURRENCIES.values()):
        raise BusinessRuleException("暂不支持这个币种")
    path = f"/v2/rate/{base.lower()}/{quote.lower()}"

    async def request(active: httpx.AsyncClient):
        response = await active.get(path, headers={"Accept": "application/json"})
        if response.status_code != 200:
            raise BusinessRuleException("外部汇率服务暂时不可用，请稍后重试")
        if len(response.content) > 32_000:
            raise BusinessRuleException("外部汇率响应异常")
        try:
            payload = response.json()
            rate = Decimal(str(payload["rate"]))
            # The as-of stamp is shown to the customer as the data's point in
            # time. Substituting our own date here would misreport when the
            # figure applied, so an untrustworthy date refuses instead.
            data_date = untrusted.require(payload.get("date"), field="汇率日期")
        except (KeyError, ValueError, TypeError, ArithmeticError):
            raise BusinessRuleException("外部汇率数据格式异常") from None
        except untrusted.UntrustedRejected:
            raise BusinessRuleException("外部汇率数据的时点标注异常，请稍后重试") from None
        if rate <= 0:
            raise BusinessRuleException("外部汇率数据无效")
        converted = (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return {
            "type": "external_data",
            "title": f"{base} / {quote} 参考汇率",
            "summary": f"{amount:,.2f} {base} ≈ {converted:,.2f} {quote}",
            "rate": str(rate),
            "amount": f"{amount:,.2f}",
            "converted": f"{converted:,.2f}",
            "base": base,
            "quote": quote,
            "as_of": data_date,
            "source": {"name": "Frankfurter 汇率 API", "url": FX_SOURCE_PAGE},
            "engine": "external-tool",
            "trace": [
                {"label": "识别需求", "detail": f"汇率查询 · {base} → {quote}", "status": "done"},
                {"label": "调用外部工具", "detail": "Frankfurter /v2/rate（仅发送币种对）", "status": "done"},
                {"label": "本地计算", "detail": "换算金额在 Nexus 本地完成", "status": "done"},
            ],
        }

    if client is not None:
        return await request(client)
    try:
        async with httpx.AsyncClient(
            base_url=FX_API_ORIGIN,
            timeout=8.0,
            follow_redirects=False,
            trust_env=False,
        ) as active:
            return await request(active)
    except BusinessRuleException:
        raise
    except (httpx.HTTPError, TimeoutError):
        raise BusinessRuleException("外部汇率服务暂时不可用，请稍后重试") from None


def capability_catalog(model_enabled: bool) -> dict:
    return {
        "agent": "Nexus",
        "principle": "先理解目标，再调用工具核验数据；写操作展示计划并等待确认。",
        "capabilities": [
            {"id": "account", "name": "账户核验", "mode": "只读", "status": "available", "prompt": "查看我的账户", "description": "读取本地账户、卡片、订阅与最近流水"},
            {"id": "bill", "name": "账单洞察", "mode": "只读", "status": "available", "prompt": "分析我这个月的消费", "description": "消费分类、异常提示、月报与年报"},
            {"id": "profile", "name": "完善经济画像", "mode": "本地资料", "status": "available", "prompt": "根据我的账户数据做一份个性化理财分析", "description": "典型画像预填或自主填写收入、资产、负债和订阅"},
            {"id": "plan", "name": "目标规划", "mode": "只读", "status": "available", "prompt": "根据我的账户数据做一份个性化理财分析", "description": "结合现金流、目标、期限与风险约束"},
            {"id": "fx", "name": "实时汇率", "mode": "外部数据", "status": "available", "prompt": "1000人民币能换多少美元", "description": "调用公开汇率 API，金额在本地换算"},
            {"id": "transfer", "name": "转账", "mode": "需确认", "status": "available", "prompt": "给张三转账100元", "description": "核对收款人和余额，确认后记账"},
            {"id": "scheduled-transfer", "name": "定时转账计划", "mode": "需确认", "status": "available", "prompt": "每月5号给张三转账1000元备注房租", "description": "创建带用途和下次日期的转账计划"},
            {"id": "card", "name": "卡片管理", "mode": "需确认", "status": "available", "prompt": "锁定尾号8826", "description": "临时锁定、解锁与挂失"},
            {"id": "subscription", "name": "订阅管理", "mode": "需确认", "status": "available", "prompt": "查看我的订阅", "description": "核对合同和代扣，支持分别取消"},
            {"id": "recurring", "name": "周期扣费识别", "mode": "只读", "status": "available", "prompt": "识别周期扣费", "description": "从本地账单识别规律扣费候选"},
            {"id": "products", "name": "产品对比", "mode": "读 / 写", "status": "available", "prompt": "对比理财产品", "description": "产品适配、申购、订单与赎回"},
            {"id": "limits", "name": "卡片限额", "mode": "需确认", "status": "available", "prompt": "把尾号8826单笔限额调到3000元", "description": "确认后调整卡片限额"},
            {"id": "macro", "name": "中国宏观指标", "mode": "官方外部数据", "status": "available", "prompt": "中国最新 GDP 增速是多少", "description": "世界银行 GDP、CPI 与失业率指标"},
            {"id": "filings", "name": "公司官方披露", "mode": "官方外部数据", "status": "available", "prompt": "苹果最新 SEC 财报", "description": "SEC EDGAR 10-K、10-Q 与 8-K 文件"},
            {"id": "universal-planner", "name": "通用多工具规划", "mode": "动态工具 + 确认", "status": "available", "prompt": "下周我要去上海出差三天，帮我一起规划", "description": "模型按目标选择最小工具集，后端读取用户数据并生成统一结构化方案"},
            {"id": "proactive-risk", "name": "主动风险守护", "mode": "事件驱动 + 确认", "status": "available", "prompt": "检查父亲账户是否有诈骗风险", "description": "读取已授权事件，给出隔离、核验与恢复步骤"},
        ],
        "tools": [
            # Names here are read by the customer, so they name banking services
            # rather than the components behind them. The data-sourcing detail
            # that must stay verifiable (provider, as_of, url) lives on each
            # answer's evidence, not on this status list.
            {"name": "账户与账单", "status": "connected", "data": "余额、卡片、订阅、流水与消费分类"},
            {"name": "智能理解", "status": "connected" if model_enabled else "optional", "data": "听懂你的说法，把一句话拆成要办的事"},
            {"name": "理财与产品", "status": "connected", "data": "持仓、产品对比、申购与赎回"},
            {"name": "支付与转账", "status": "connected", "data": "转账、定期计划、AA 收款与代扣"},
            {"name": "用卡与安全", "status": "connected", "data": "锁定挂失、限额调整与风险提醒"},
            {"name": "外汇与市场数据", "status": "connected", "data": "实时汇率、宏观指标与公司公开披露"},
        ],
        "data_policy": "公开数据 API 只接收查询代码；启用远程 Tool Agent 时，完成请求所需的最小化工具结果可能进入模型上下文。",
        "limitations": ["不连接真实银行账户", "不提供实时证券交易行情", "不自动执行资金和账户状态变更"],
    }
