"""Read-only module dispatcher: model proposes, Decimal rules verify and aggregate."""
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
import logging
import re
from ..contracts.financial_plan import FinancialTaskPlan, MODULES

log = logging.getLogger(__name__)
D = Decimal
ZERO = D('0')

def cash(value):
    return D(str(value).replace('¥', '').replace(',', ''))

def money(value):
    return f'¥{value:,.2f}'

def amount_from(text):
    match = re.search(r'(\d+(?:\.\d+)?)\s*(亿|万|千)?\s*(?:元|块)?', text)
    if not match:
        numbers = {'一百万':'1000000', '一百万元':'1000000', '十万':'100000', '百万':'1000000'}
        return next((D(value) for word, value in numbers.items() if word in text), None)
    return D(match[1]) * {'亿':D('100000000'), '万':D('10000'), '千':D('1000'), None:D('1')}[match[2]]

def fallback_plan(message):
    """Only unambiguous amount + duration + explicit goal; other text asks for input."""
    if re.search(r'不(?:要|再|想|考虑).{0,8}(?:赚|盈利|攒|存)',message):
        return None
    target = re.search(r'(?:赚取?|盈利|获利|收益达到|攒到?|存到?|积累)\s*([\d.]+\s*[亿万千]?\s*(?:元|块)?|一百万|百万|十万)', message)
    if not target:
        return None
    periods = list(re.finditer(r'(\d+|一个|一|两|二|三|六|十二)\s*(个月|月|年)', message[:target.start()]))
    period = periods[-1] if periods else re.search(r'(\d+|一个|一|两|二|三|六|十二)\s*(个月|月|年)', message[target.end():])
    months = None
    if period:
        n = {'一个':1,'一':1,'两':2,'二':2,'三':3,'六':6,'十二':12}.get(period[1])
        n = n if n is not None else int(period[1])
        months = n * (12 if period[2] == '年' else 1)
    if months is None and not re.search(r'赚|盈利|获利|收益',target[0]):
        return None
    return FinancialTaskPlan(source='bounded-fallback', objective=message[:160], evidence=message, amount=str(amount_from(target[1])), months=months, kind='profit' if re.search(r'赚|盈利|获利|收益',target[0]) else 'savings', confidence=0.5 if len(re.findall(r'(?:赚|盈利|攒|存到)\s*(?:[0-9]|一百万|百万|十万)',message)) > 1 else 1.0, modules=list(MODULES))

def resolve_plan(message, proposed=None):
    fallback = fallback_plan(message)
    if proposed:
        try:
            plan = FinancialTaskPlan.model_validate(proposed)
        except ValueError:
            return None
        # A model cannot invent a current objective or silently change its numbers.
        if plan.evidence not in message:
            return None
        if plan.amount and (fallback is None or cash(plan.amount) != cash(fallback.amount)):
            return None
        if plan.months and (fallback is None or plan.months != fallback.months):
            return None
        if fallback and fallback.confidence < 0.8:
            plan.confidence = min(plan.confidence, fallback.confidence)
        if fallback:
            plan.kind = fallback.kind
        return plan
    return fallback

def calculate_goal(plan, baseline):
    cash_total = cash(baseline['balance_sheet']['cash'])
    emergency = cash(baseline['allocation']['buckets'][0]['target'])
    principal = max(cash_total - emergency, ZERO)
    surplus = cash(baseline['cashflow']['surplus'])
    target = cash(plan.amount)
    months = D(plan.months)
    gap = target if plan.kind == 'profit' else max(target - principal, ZERO)
    required_monthly = (gap / months).quantize(D('.01'), rounding=ROUND_HALF_UP)
    rate = target / principal * 100 if principal > 0 and plan.kind == 'profit' else None
    # Screening threshold, not a forecast or a guaranteed achievable return.
    extreme = plan.kind == 'profit' and (rate is None or rate / months > D('1'))
    score = min(max(surplus, ZERO) / required_monthly * 20, D('20')) if required_monthly else D('20')
    return {'status':'EXTREME' if extreme else 'REVIEW_REQUIRED' if plan.kind == 'profit' else 'ON_TRACK' if surplus >= required_monthly else 'GAP', 'principal':money(principal), 'target':money(target), 'required_monthly':money(required_monthly), 'period_return_pct':str(rate.quantize(D('.01'))) if rate is not None else None, 'goal_score':0.0 if extreme else float(score.quantize(D('.1'))), 'months':plan.months, 'kind':plan.kind, 'basis':'本金只取现金扣除应急金；不假设持仓可立即赎回，不使用借款。收益率为期内总收益率；月均超过1%只触发保守审核，不代表低于此值可保证实现。'}

def risk_module(message, goal, baseline):
    flags=[]
    if goal['status']=='EXTREME': flags.append('EXTREME_RETURN')
    if re.search(r'借钱|借款|贷款.*(?:投资|理财)|加杠杆',message): flags.append('BORROW_TO_INVEST')
    if re.search(r'稳赚|保本高收益|保证.*收益|无风险.*收益|稳定.*百万',message): flags.append('RETURN_PROMISE')
    if re.search(r'R[3-5]',message,re.I): flags.append('HIGH_RISK_PRODUCT')
    from ..analysis.financial_analysis import _risk_cap_detail
    profile = baseline['profile']
    current_cap = _risk_cap_detail(profile.get('risk_score',''), D(profile.get('max_drawdown_pct','0')), goal['months'])
    cap = min(current_cap['cap'], 2, 1 if goal['months']<=12 else 2)
    return {'flags':flags,'blocked':bool(flags) or goal['kind']=='profit','max_product_risk':f'R{cap}', 'reason':'极端收益、借款投资、收益承诺或R3及以上请求禁止产品推荐；盈利目标须先核实可行性。'}

def aggregate(plan, baseline, saved_goal, message):
    goal=calculate_goal(plan,baseline)
    risk=risk_module(message,goal,baseline)
    conflict=bool(saved_goal and saved_goal != plan.objective and plan.kind != 'allocation')
    # All mandatory safety modules run even when the model omits them.
    requested=list(dict.fromkeys(plan.modules + list(MODULES)))
    products=[]
    if not conflict and not risk['blocked'] and goal['status']=='ON_TRACK':
        for item in baseline.get('product_matches',[]):
            if int(item['risk_level'][1:]) <= int(risk['max_product_risk'][1:]) and item.get('lock_days',0)<=plan.months*28 and cash(item['min_purchase'])<=cash(goal['principal']):
                products.append(item)
    verdict=('按当前本金，不能把这个目标作为合法、低风险、稳定的理财计划来承诺实现；不推荐用产品追逐该收益。' if goal['status']=='EXTREME' else '先核实目标与风险约束，再决定是否配置产品。' if risk['blocked'] else '按当前现金流测算此储蓄目标。')
    alternatives=['延长周期或下调目标金额。','通过增加收入、减少非必要支出积累本金，保留应急金。','核对负债利率，比较确定的还债成本节省与非保证的投资参考收益；不借钱投资。']
    impact=f'旧目标“{saved_goal}”仅作背景，未沿用其金额或月度计划。请确认保留、延后还是替换原目标；确认前不修改档案。' if conflict else '本次分析不自动修改已保存档案。'
    handlers={
        'goal_calculation':lambda:goal,
        'financial_diagnosis':lambda:dict(baseline['balance_sheet']),
        'cashflow':lambda:dict(baseline['cashflow']),
        'risk_compliance':lambda:risk,
        'product_matching':lambda:{'status':'BLOCKED' if risk['blocked'] else 'REVIEWED','products':products},
        'original_goal_impact':lambda:{'conflict':conflict,'message':impact},
    }
    results={name:{'status':'done','data':handlers[name]()} for name in requested}
    consistency=not (risk['blocked'] and products) and all(int(p['risk_level'][1:])<=int(risk['max_product_risk'][1:]) for p in products)
    if not consistency:
        products=[]
        verdict='模块结果未通过一致性校验，暂停产品建议，请补充资料或转人工核验。'
    trace=[{'label':'任务拆解','detail':', '.join(requested),'status':'done'}, {'label':'规则测算','detail':goal['basis'],'status':'done'}, {'label':'风险门禁','detail':', '.join(risk['flags']) or '未发现已知红线，收益仍不保证','status':'done'}, {'label':'一致性校验','detail':'当前目标、金额与产品风险校验通过' if consistency else '校验失败，停止推荐','status':'done'}]
    log.info('financial_orchestration kind=%s status=%s modules=%s flags=%s conflict=%s',plan.kind,goal['status'],requested,risk['flags'],conflict)
    return {
        'type':'financial_analysis','engine':'composite-analysis','title':'当前目标的可行性与风险分析','request_focus':'current_goal',
        'as_of':datetime.now().date().isoformat(), 'summary':verdict, 'profile':{'goal':plan.objective,'horizon_months':plan.months,'style':baseline['profile'].get('style','待评估'),'risk_score':baseline['profile']['risk_score'],'data_quality':'当前目标覆盖旧目标；资产和现金流来自已保存资料'},
        'recommendation':{'verdict':verdict,'investable':{'amount':goal['principal'],'basis':'现金扣除应急金的测算本金，不代表授权投资'},'risk_cap':{'level':risk['max_product_risk'],'reason':'当前目标期限、风险测评与规则风控','constraints':[]},'buy':products,'avoid':[{'subject':'追逐高收益或借钱投资','reason':risk['reason']}] if risk['blocked'] else [],'actions':alternatives,'debt_comparison':None},
        'metrics':[{'label':'当前目标金额','value':goal['target'],'basis':'用户本次显式目标'},{'label':'测算本金','value':goal['principal'],'basis':goal['basis']},{'label':'期内所需收益率','value':(goal['period_return_pct']+'%') if goal['period_return_pct'] is not None else '无本金或非盈利目标，无法计算','basis':'利润目标 / 测算本金，非年化收益率'},{'label':'月度可持续结余','value':baseline['cashflow']['surplus'],'basis':'已保存资料的现金流规则计算'}],
        'balance_sheet':baseline['balance_sheet'],'cashflow':baseline['cashflow'],
        'allocation':{'method':'先验证当前目标可行性；旧目标资金不自动释放，未确认前不执行配置。','buckets':[]},
        'decision':{'goal_feasibility':goal['status'],'monthly_required':goal['required_monthly'],'monthly_surplus':baseline['cashflow']['surplus'],'max_product_risk':risk['max_product_risk']},
        'product_matches':products,'product_rejected':[], 'observations':[goal['basis'],impact], 'narrative':None,
        'orchestration':{'source':plan.source,'objective':plan.objective,'confidence':plan.confidence,'complexity':'compound','modules':requested,'results':results,'consistent':consistency,'goal_conflict':conflict},
        'health':{'score':None,'label':'按当前目标单独评估','components':[{'name':'当前目标可行性','score':goal['goal_score'],'max':20,'basis':'当前目标所需月度金额与月结余比较；盈利仍需风险审核'}]},
        'needs_input':conflict,'follow_up':impact,'trace':trace,
    }
