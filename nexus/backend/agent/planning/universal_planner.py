"""General financial task planner: model-selected read tools, local execution, one response schema."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import re
from sqlalchemy import select, or_

from ...core.models import Account, Card, ContextEvent, FinancialProfile, FinancialSnapshot, OrchestratedPlan, Product, Recipient, StatementTransaction, User
from ...services.subscription_service import SubscriptionService
from ...services.product_service import ProductService
from ..analysis import analytics
from ..analysis import charts
from ..integrations import model
from ..contracts.understanding import READ_TOOLS


def _fallback_tools(text: str) -> list[str]:
    """Capability routing fallback, grouped by data domain rather than named demo scripts."""
    mapping = {
        "account": ("转账","余额","预算","首付","理财","安排","规划","诈骗","盗刷"),
        "bills": ("聚餐","消费","外卖","预算","预测","账单","偏好","机票"),
        "recipients": ("转账","红包","生活费","聚餐"),
        "calendar": ("生日","下个月","下周","出差","旅行"),
        "social_context": ("聚餐","群聊","生日","红包","爱人","老婆","妈妈","老王"),
        "income_events": ("工资","发工资","收入"),
        "cards": ("卡","盗刷","机票","酒店","额度","支付"),
        "card_benefits": ("权益","机场","贵宾厅","机票","酒店","旅行","出差"),
        "subscriptions": ("订阅","会员","代扣","清理","平替"),
        "subscription_usage": ("未使用","清理","家庭订阅","家人订阅","比价","平替"),
        "financial_profile": ("理财","首付","目标","预测","预算","调仓"),
        "products": ("理财","调仓","降息","收益","稳健"),
        "market_events": ("降息","市场","异动","调仓"),
        "travel_context": ("机票","出差","旅行","上海","日本","天气","酒店"),
        "family_risk": ("长辈","父亲","母亲","亲情","诈骗"),
    }
    tools=[tool for tool,words in mapping.items() if any(word in text for word in words)]
    return tools[:8] or ["account","bills"]


async def _events(session, user_id: int, types: tuple[str,...]) -> list[ContextEvent]:
    return list((await session.scalars(select(ContextEvent).where(ContextEvent.user_id==user_id,ContextEvent.event_type.in_(types),ContextEvent.status=="ACTIVE").order_by(ContextEvent.occurred_at.desc()))).all())


EVENT_FIELD_LABELS = {
    "merchant":"商户", "total":"总额", "participants":"参与人", "payer":"垫付人", "user_share":"应付份额",
    "contact":"联系人", "date":"日期", "recent_average":"近期往来均值", "suggested":"建议金额",
    "day":"每月到账日", "amount":"金额", "employer":"单位", "time":"时间", "city":"城市",
    "card_last4":"卡尾号", "new_device":"新设备", "destination":"目的地", "departure_date":"出发日",
    "weather":"天气", "preferred_transport":"偏好交通", "hotel_benefit":"酒店权益",
    "temporary_limit_needed":"建议临时额度", "relation":"关系", "recipient":"收款方", "status":"状态",
    "days_unused":"未使用天数", "next_fee":"下次费用", "next_charge":"续费日", "current":"当前方案",
    "current_price":"当前月费", "alternative":"替代方案", "alternative_price":"替代月费", "saving_year":"预计年省",
    "orders":"订单数", "baseline_orders":"平时订单数", "dining_budget_used_pct":"餐饮预算使用率",
    "current_spend":"当前支出", "projected_spend":"预测月底支出", "credit_limit":"信用额度",
    "projected_over":"预测超额", "confidence_pct":"预测置信度", "lounge_visits":"贵宾厅次数",
    "fast_track":"快速安检", "valid_until":"有效期", "current_fx_fee_pct":"当前外币费率",
    "recommended_card":"建议卡片", "deviation_pct":"偏离日常幅度",
}


def _display_event(event: ContextEvent) -> str:
    parts = []
    for key, value in event.payload.items():
        if key not in EVENT_FIELD_LABELS:
            continue
        label = EVENT_FIELD_LABELS[key]
        if isinstance(value, list):
            rendered = "、".join(str(item) for item in value)
        elif isinstance(value, bool):
            rendered = "是" if value else "否"
        else:
            rendered = str(value)
        if key in {"total", "user_share", "amount", "suggested", "recent_average", "next_fee", "current_price", "alternative_price", "saving_year", "temporary_limit_needed", "current_spend", "projected_spend", "credit_limit", "projected_over"}:
            rendered = f"¥{Decimal(str(value)):,.2f}"
        elif key.endswith("_pct"):
            rendered = f"{value}%"
        parts.append(f"{label} {rendered}")
    return f"{event.title}：" + "，".join(parts[:7])


async def _render_summary(message: str, observations: list[dict], planner: str) -> str:
    """Render summary via LLM (preferred) or template (fallback). Never blocks on LLM failure."""
    template = f"已由{planner}选择并调用 {len(observations)} 个用户数据工具。以下数字来自你的账户与已授权数据。"
    if not observations:
        return template
    facts = "\n".join(f"- {o['label']}：{o['summary']}" for o in observations[:6])
    prompt = (
        f"基于以下已验证事实，用 1 句中文向用户说明他/她的财务状况要点。"
        f"要求：不超过 80 字、不编造数字、不使用列表。\n"
        f"用户问题：{message}\n"
        f"事实：\n{facts}\n"
        f"输出："
    )
    rendered = await model.summarize_text(prompt, max_tokens=100)
    return rendered or template


def _unresolved_missing(items: list[str], observations: list[dict]) -> list[str]:
    """Remove pre-tool uncertainties once an executed tool returned the required fact."""
    available = {item["tool"] for item in observations if item.get("data")}
    domains = {
        "account": ("余额", "可用资金", "账户"),
        "bills": ("账单", "交易记录", "消费记录", "消费金额"),
        "recipients": ("收款人", "账户信息", "手机号"),
        "calendar": ("日期", "日程", "时间"),
        "social_context": ("聚餐", "参与人", "垫付", "金额", "关系", "偏好", "AA", "分摊", "全额"),
        "income_events": ("工资", "收入", "到账日"),
        "cards": ("卡片", "卡号", "额度"),
        "subscriptions": ("订阅", "会员", "代扣"),
        "financial_profile": ("目标", "负债", "风险偏好", "期限"),
        "products": ("产品", "收益", "锁定期"),
        "travel_context": ("行程", "天气", "酒店", "交通"),
        "family_risk": ("长辈", "父亲", "母亲", "可疑交易", "诈骗"),
    }
    unresolved = []
    for item in items:
        if "紧急程度" in item:
            continue
        resolved = any(tool in available and any(word in item for word in words) for tool, words in domains.items())
        if not resolved:
            unresolved.append(item)
    return unresolved


async def _run_tool(session, user_id: int, tool: str, message: str = "") -> dict:
    if tool=="account":
        rows=list((await session.scalars(select(Account).where(Account.user_id==user_id))).all());available=sum((Decimal(r.available_balance) for r in rows),Decimal("0"));reserved=sum((Decimal(r.reserved_balance) for r in rows),Decimal("0"))
        return {"tool":tool,"label":"账户工具","summary":f"可用余额 ¥{available:,.2f}，计划预留 ¥{reserved:,.2f}","metrics":[("可用余额",f"¥{available:,.2f}"),("计划预留",f"¥{reserved:,.2f}")],"data":{"available":float(available),"reserved":float(reserved)}}
    if tool=="bills":
        rows=list((await session.scalars(select(StatementTransaction).where(StatementTransaction.user_id==user_id).order_by(StatementTransaction.txn_date.desc()).limit(60))).all());total=sum((abs(Decimal(r.amount or 0)) for r in rows),Decimal("0"));anomalies=[r for r in rows if r.is_anomaly]
        dining=sum((abs(Decimal(r.amount or 0)) for r in rows if r.category=="餐饮"),Decimal("0"))
        signals=await _events(session,user_id,("LATE_NIGHT_DELIVERY","SPENDING_FORECAST"));signal_text="；".join(_display_event(e) for e in signals)
        summary=f"读取 {len(rows)} 笔近期账单，餐饮 ¥{dining:,.2f}，需关注 {len(anomalies)} 笔"+(f"；{signal_text}" if signal_text else "")
        metrics=[("近期账单",f"{len(rows)} 笔"),("餐饮支出",f"¥{dining:,.2f}"),("需关注",f"{len(anomalies)} 笔")]
        # A picture earns its place here because "did I spend more or less than
        # last month" is a question about the gap, and prose hides the gap.
        flow=await analytics.monthly_cashflow(session,user_id)
        comparison=flow.get("comparison")
        if comparison:
            metrics.append((f"{comparison['to']} 较 {comparison['from']} 支出",f"¥{comparison['expense_delta']:+,.2f}"))
            if comparison.get("net_delta") is not None:
                metrics.append((f"{comparison['to']} 较 {comparison['from']} 结余",f"¥{comparison['net_delta']:+,.2f}"))
        return {"tool":tool,"label":"账单分析","summary":summary,"metrics":metrics,"chart":flow.get("chart"),"data":{"count":len(rows),"total":float(total),"dining":float(dining),"anomalies":len(anomalies),"comparison":comparison,"signals":[{"type":e.event_type,**e.payload} for e in signals]}}
    if tool=="recipients":
        rows=list((await session.scalars(select(Recipient).where(Recipient.user_id==user_id))).all())
        return {"tool":tool,"label":"收款人工具","summary":"已登记 "+"、".join(r.name for r in rows),"metrics":[("可用收款人",f"{len(rows)} 位")],"data":{"items":[{"id":r.id,"name":r.name,"alias":r.alias} for r in rows]}}
    if tool=="cards":
        accounts=list((await session.scalars(select(Account.id).where(Account.user_id==user_id))).all());rows=list((await session.scalars(select(Card).where(Card.account_id.in_(accounts)))).all())
        return {"tool":tool,"label":"卡片工具","summary":"；".join(f"尾号 {r.last4} {r.status}，单笔限额 ¥{r.single_limit:,.0f}" for r in rows),"metrics":[("可用卡片",f"{len(rows)} 张")],"data":{"items":[{"last4":r.last4,"status":r.status,"single_limit":float(r.single_limit or 0)} for r in rows]}}
    if tool=="subscriptions":
        rows=await SubscriptionService(session).list_user_subscriptions(user_id);monthly=sum((Decimal(str(r["amount"])) for r in rows if r["status"]=="ACTIVE"),Decimal("0"))
        return {"tool":tool,"label":"订阅工具","summary":f"{len(rows)} 项已连接订阅，每月 ¥{monthly:,.2f}","metrics":[("连接订阅",f"{len(rows)} 项"),("月度扣费",f"¥{monthly:,.2f}")],"data":{"items":rows}}
    if tool=="financial_profile":
        p=await session.scalar(select(FinancialProfile).where(FinancialProfile.user_id==user_id));s=await session.scalar(select(FinancialSnapshot).where(FinancialSnapshot.user_id==user_id))
        if not p:return {"tool":tool,"label":"财务画像","summary":"尚未完善收入、负债和目标资料","metrics":[],"data":{}}
        surplus=Decimal(p.monthly_income)-Decimal(p.essential_expenses)-Decimal(p.monthly_debt_payment)
        seasonal=await analytics.seasonal_cashflow(session,user_id)
        return {"tool":tool,"label":"财务画像","summary":f"目标“{p.goal_name}”，期限 {p.horizon_months} 个月；月度基础结余 ¥{surplus:,.2f}","metrics":[("月收入",f"¥{p.monthly_income:,.2f}"),("基础结余",f"¥{surplus:,.2f}"),("目标期限",f"{p.horizon_months} 月")],"chart":seasonal.get("chart"),"data":{"goal":p.goal_name,"goal_amount":float(p.goal_amount),"goal_saved":float(p.goal_saved),"surplus":float(surplus),"investments":float(s.investment_assets) if s else 0}}
    if tool=="products":
        user=await session.get(User,user_id);rows=await ProductService(session).list_products(user_id)
        trend=await analytics.product_trend(session,rows)
        metrics=[("风险等级",user.risk_score or "未测评"),("候选产品",f"{len(rows)} 个")]
        movers=[item for item in trend["by_code"].items() if item[1].get("window_change_pct") is not None]
        if movers:
            code,info=max(movers,key=lambda row: abs(row[1]["window_change_pct"]))
            metrics.append((f"{code} 近 {trend['window_days']} 天",f"{info['window_change_pct']:+.2f}%"))
        return {"tool":tool,"label":"产品工具","summary":f"按 {user.risk_score or '未测评'} 风险等级核验 {len(rows)} 个在售产品","metrics":metrics,"chart":trend.get("chart"),"data":{"items":[{"code":r["code"],"name":r["name"],"risk":r["risk_level"],"rate":float(r["yield_rate"]),"lock_days":r["lock_days"],"window_change_pct":trend["by_code"].get(r["code"],{}).get("window_change_pct"),"recent_change_pct":trend["by_code"].get(r["code"],{}).get("recent_change_pct"),"direction":trend["by_code"].get(r["code"],{}).get("direction","flat")} for r in rows]}}
    event_types={
        "calendar":("SPOUSE_BIRTHDAY","CONTACT_BIRTHDAY"),"social_context":("GROUP_DINNER","CONTACT_BIRTHDAY","SPOUSE_BIRTHDAY"),
        "income_events":("SALARY",),"card_benefits":("CARD_BENEFIT","FLIGHT_PURCHASE"),
        "subscription_usage":("SUBSCRIPTION_USAGE","SUBSCRIPTION_OFFER","HOUSEHOLD_SUBSCRIPTIONS"),
        "market_events":("MARKET_EVENT","INVESTMENT_MATURITY"),"travel_context":("TRAVEL_PLAN","FLIGHT_PURCHASE"),
        "family_risk":("FAMILY_RISK","FRAUD_TRANSACTION"),
    }
    if tool == "calendar" and any(word in message for word in ("出差", "旅行", "行程", "下周")):
        event_types[tool] = ("TRAVEL_PLAN",)
    elif tool == "social_context" and any(word in message for word in ("聚餐", "AA")):
        event_types[tool] = ("GROUP_DINNER",)
    elif tool == "social_context" and any(word in message for word in ("生日", "红包")):
        event_types[tool] = ("CONTACT_BIRTHDAY", "SPOUSE_BIRTHDAY")
    elif tool == "card_benefits" and any(word in message for word in ("上海", "出差")):
        event_types[tool] = ("CARD_BENEFIT",)
    elif tool == "travel_context" and any(word in message for word in ("上海", "出差")):
        event_types[tool] = ("TRAVEL_PLAN",)
    elif tool == "travel_context" and any(word in message for word in ("日本", "机票", "外币")):
        event_types[tool] = ("FLIGHT_PURCHASE",)
    elif tool == "family_risk" and any(word in message for word in ("父亲", "母亲", "长辈", "亲情")):
        event_types[tool] = ("FAMILY_RISK",)
    elif tool == "family_risk" and any(word in message for word in ("盗刷", "异地")):
        event_types[tool] = ("FRAUD_TRANSACTION",)
    rows=await _events(session,user_id,event_types[tool]);summary="；".join(_display_event(r) for r in rows) or "暂无已授权事件"
    return {"tool":tool,"label":{"calendar":"日程工具","social_context":"社交语境","income_events":"收入事件","card_benefits":"卡片权益","subscription_usage":"订阅使用","market_events":"市场事件","travel_context":"差旅工具","family_risk":"家庭风控"}[tool],"summary":summary,"metrics":[("有效事件",f"{len(rows)} 条")],"data":{"events":[{"type":r.event_type,"title":r.title,**r.payload} for r in rows]}}


def _recommend_actions(message: str, observations: list[dict]) -> list[dict]:
    by={o["tool"]:o for o in observations};actions=[]
    social=[e for e in by.get("social_context",{}).get("data",{}).get("events",[])]
    recipients=by.get("recipients",{}).get("data",{}).get("items",[])
    dinner=next((e for e in social if e.get("type")=="GROUP_DINNER"),None)
    if dinner and any(w in message for w in ("聚餐", "AA")):
        payer = str(dinner.get("payer") or "")
        share = Decimal(str(dinner.get("user_share") or 0))
        if payer and share > 0 and any(r["name"] == payer or r.get("alias") == payer for r in recipients):
            actions.append({"label":f"生成 ¥{share:,.0f} AA 转账确认","command":f"给{payer}转账{share:.2f}元备注昨晚聚餐AA","tone":"primary"})
    income=by.get("income_events",{}).get("data",{}).get("events",[])
    if income and any(w in message for w in ("工资","发工资")):
        match=re.search(r"给([^，,。\s]{1,12}?)(?:转|发)([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|块|块钱)",message)
        if match and any(r["name"]==match.group(1) or r.get("alias")==match.group(1) for r in recipients):
            salary_day=int(income[0].get("day",28));actions.append({"label":f"生成工资日 ¥{Decimal(match.group(2)):,.0f} 转账计划","command":f"每月{salary_day}号给{match.group(1)}转账{match.group(2)}元备注生活费","tone":"primary"})
    birthday=next((e for e in social if e.get("type")=="CONTACT_BIRTHDAY"),None)
    if birthday and "红包" in message:
        contact = str(birthday.get("contact") or "")
        suggested = Decimal(str(birthday.get("suggested") or birthday.get("recent_average") or 0))
        if contact and suggested > 0 and any(r["name"] == contact or r.get("alias") == contact for r in recipients):
            actions.append({"label":f"生成 ¥{suggested:,.0f} 红包确认","command":f"给{contact}转账{suggested:.2f}元备注生日红包","tone":"primary"})
    cards=by.get("cards",{}).get("data",{}).get("items",[])
    risk=by.get("family_risk",{}).get("data",{}).get("events",[])
    fraud = next((event for event in risk if event.get("type") == "FRAUD_TRANSACTION"), None)
    risk_last4 = str(fraud.get("card_last4")) if fraud and fraud.get("card_last4") else ""
    if risk_last4 and any(card["last4"] == risk_last4 for card in cards) and any(w in message for w in ("盗刷", "异地", "异常")):
        actions.append({"label":f"先临时锁定尾号 {risk_last4}","command":f"锁定尾号{risk_last4}","tone":"danger"})
    travel_events = by.get("travel_context",{}).get("data",{}).get("events",[]) + by.get("card_benefits",{}).get("data",{}).get("events",[])
    flight = next((event for event in travel_events if event.get("type") == "FLIGHT_PURCHASE"), None)
    recommended_card = str(flight.get("recommended_card") or "") if flight else ""
    if recommended_card and any(w in message for w in ("机票", "旅行", "出差", "外币")):
        actions.append({"label":f"生成{recommended_card}申请确认","command":f"申请一张{recommended_card}","tone":"primary"})
    trip = next((event for event in travel_events if event.get("type") == "TRAVEL_PLAN"), None)
    benefit = next((event for event in travel_events if event.get("type") == "CARD_BENEFIT"), None)
    if trip and benefit:
        limit = Decimal(str(trip.get("temporary_limit_needed") or 0))
        last4 = str(benefit.get("card_last4") or "")
        card = next((item for item in cards if item["last4"] == last4), None)
        if card and limit > Decimal(str(card["single_limit"])):
            actions.append({"label":f"将尾号 {last4} 单笔限额调至 ¥{limit:,.0f}","command":f"把尾号{last4}单笔限额调到{limit:.2f}元","tone":"primary"})
    subscription_events = by.get("subscription_usage",{}).get("data",{}).get("events",[])
    connected = by.get("subscriptions",{}).get("data",{}).get("items",[])
    offer = next((event for event in subscription_events if event.get("type") == "SUBSCRIPTION_OFFER"), None)
    current = str(offer.get("current") or "") if offer else ""
    if current and any(item.get("merchant_name") == current and item.get("status") == "ACTIVE" for item in connected):
        actions.append({"label":f"生成取消{current}确认","command":f"取消{current}订阅","tone":"primary"})
    products=by.get("products",{}).get("data",{}).get("items",[])
    if products and any(w in message for w in ("稳健","理财","调仓","降息")):
        best=sorted(products,key=lambda x:(x["risk"],-x["rate"],x["lock_days"]))[0]
        actions.append({"label":f"查看 {best['code']} 申购确认","command":f"申购 {best['code']} 100元","tone":"primary"})
    return actions[:3]


def _professional_recommendations(observations: list[dict], urgency: str, missing: list[str]) -> list[dict]:
    by={o["tool"]:o for o in observations};items=[]
    if urgency=="urgent":items.append({"title":"先隔离风险，再核验交易","detail":"暂时限制可疑支付路径，保留交易证据；用户确认前不做挂失、报警或资金转移。","badge":"立即关注"})
    if "account" in by:items.append({"title":"以可用余额作为执行上限","detail":by["account"]["summary"]+"。任何计划执行前都会重新检查余额与预留。","badge":"资金约束"})
    if "bills" in by:items.append({"title":"把消费趋势纳入决策","detail":by["bills"]["summary"]+"。异常只作为复核信号，不直接定性为欺诈或情绪问题。","badge":"行为证据"})
    if "financial_profile" in by:items.append({"title":"优先保护目标与安全垫","detail":by["financial_profile"]["summary"]+"。长期配置不能挤占近期刚性支出。","badge":"目标约束"})
    if "social_context" in by:items.append({"title":"用已授权语境补齐业务参数","detail":by["social_context"]["summary"]+"。场景事实只用于本次规划，金额和对象仍会在确认卡中再次展示。","badge":"语境核验"})
    if "products" in by:items.append({"title":"收益、流动性与风险一起比较","detail":by["products"]["summary"]+"。展示收益差异时同时说明锁定期和风险等级。","badge":"适当性"})
    if "travel_context" in by or "card_benefits" in by:items.append({"title":"先用已有权益，再新增产品","detail":"优先匹配当前卡片的酒店、贵宾厅和支付权益；不足部分才进入提额或申卡确认。","badge":"差旅优化"})
    if "subscription_usage" in by:items.append({"title":"按使用率和全年成本清理","detail":"低使用订阅先核对数据与到期日，替换和取消分成两个独立动作，避免服务中断。","badge":"订阅优化"})
    if missing:items.append({"title":"补齐后再执行","detail":"；".join(missing),"badge":"待补充"})
    return items[:5] or [{"title":"按目标分步执行","detail":"先读取用户事实，再处理资金、账户状态和外部服务；每个写动作独立确认。","badge":"执行原则"}]


async def build_universal_plan(
    session, user_id: int, message: str, *, tools: list[str] | None = None
) -> dict:
    """Render a multi-tool answer.

    When ``tools`` is provided the caller (the understanding layer) has already
    decided which data the question needs, so this function must not re-ask the
    model to plan — it only fetches, renders, and phrases. Without it, the model
    plans the tool set itself, which is the fallback path.
    """
    objective: str = ""
    missing: list[str] = []
    urgency: str = "normal"
    planner: str = ""
    if tools is not None:
        selected = [t for t in dict.fromkeys(tools) if t in READ_TOOLS][:8]
        if not selected:
            selected = _fallback_tools(message)
        tools = selected
        objective = message[:120]
        planner = "意图理解层选定工具"
    else:
        tools = []
        try:
            model_plan = await model.plan_read_tools(message)
            tools = list(dict.fromkeys(_fallback_tools(message) + model_plan.tools))[:8]
            objective = model_plan.objective or message[:120]
            missing = model_plan.missing_information or []
            urgency = model_plan.urgency or "normal"
            planner = "当前模型 动态规划 + 本地能力校验"
        except model.ModelUnavailable:
            # fallback 路径：完全用本地能力，仍要返回真实数字与建议
            tools = _fallback_tools(message)
            objective = message[:120]
            missing = []
            urgency = "urgent" if any(w in message for w in ("盗刷", "诈骗", "异常")) else "normal"
            planner = "本地能力路由（模型暂不可用）"
    observations=[await _run_tool(session,user_id,tool,message) for tool in tools]
    missing = _unresolved_missing(missing, observations)
    # 用 LLM 自然语言化 summary（不可用时退化到模板）
    summary = "；".join(o["summary"] for o in observations[:3]) + "。具体建议与依据见下方。"
    metrics=[]
    for o in observations:
        for label,value in o.get("metrics",[]):
            if not any(m["label"]==label for m in metrics):metrics.append({"label":label,"value":value,"tone":"warn" if any(w in label for w in ("关注","风险","预留")) else "normal"})
    steps=[{"tool":o["label"],"action":"读取当前用户授权数据","observation":o["summary"],"status":"done"} for o in observations]
    steps.append({"tool":"Nexus 决策层","action":"交叉核验并生成方案","observation":f"基于 {len(observations)} 个工具结果；写操作仍需逐项确认","status":"ready"})
    evidence=[{"source":o["label"],"label":o["tool"],"value":o["summary"]} for o in observations]
    # Several read tools can return a curve. Only one goes on top of the answer,
    # chosen by how much movement it actually shows, so the reply leads with the
    # comparison the user is really asking about instead of a decorative chart.
    candidates=[o["chart"] for o in observations if o.get("chart")]
    chart=charts.pick_chart(candidates)
    recommendations=_professional_recommendations(observations,urgency,missing)
    plan=OrchestratedPlan(user_id=user_id,scenario_id="universal",title="多工具金融任务",objective=objective,status="DRAFT",steps=steps,evidence=evidence)
    session.add(plan);await session.flush()
    actions=_recommend_actions(message,observations)
    actions.append({"label":"保存这份方案","command":f"启用方案#{plan.id}","tone":"secondary"})
    # Every plan must offer at least one concrete follow-up, but it has to be
    # about the data this plan actually read — a generic "分析本月账单" button on
    # a transfer question reads as filler and erodes trust in the trace.
    if len(actions) <= 1:
        read_tools = {item["tool"] for item in observations if item.get("data")}
        follow_up = None
        if "recipients" in read_tools:
            follow_up = {"label": "查看账户", "command": "查看账户", "tone": "secondary"}
        elif "bills" in read_tools:
            follow_up = {"label": "查看本月账单", "command": "分析我这个月的账单", "tone": "secondary"}
        elif "cards" in read_tools:
            follow_up = {"label": "查看我的卡片", "command": "查看我的卡片", "tone": "secondary"}
        elif "subscriptions" in read_tools:
            follow_up = {"label": "查看我的订阅", "command": "查看我的订阅", "tone": "secondary"}
        elif "account" in read_tools:
            follow_up = {"label": "查看账户余额", "command": "查看余额", "tone": "secondary"}
        actions.insert(0, follow_up or {"label": "查看账户余额", "command": "查看余额", "tone": "secondary"})
    return {"type":"universal_plan","plan_id":plan.id,"title":"Nexus 多工具决策方案","subtitle":objective,"summary":summary,"urgency":urgency,"chart":chart,"metrics":metrics[:8],"steps":steps,"evidence":evidence,"recommendations":recommendations,"actions":actions,"engine":"analysis"}
