"""规划器对"这件事到底办不办得成"有最终解释权。

走到 clarify 只说明*模型*觉得少了东西，不说明后端真的缺。如果 plan_builder
能用用户自己的登记数据把计划建出来，再去问"还需要收款人"，就是在问用户他明明
给过的东西——而且那张卡上原本还挂着"已核验你给的信息"，等于替一句可能不成立的
话背书。

模型的自报（missing_information / output="clarify"）是**提议**，不是结论；结论由
只读的规划器给。这组测试直接构造"模型说缺、槽位其实齐全"的输入，不依赖模型当时
的心情。
"""
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from nexus.backend.agent import model
from nexus.backend.agent.understanding import Understanding
from nexus.backend.core.models import DemoAction, Transaction


async def say(client, text):
    response = await client.post('/api/messages', json={'message': text, 'request_id': str(uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


def over_reporting(**overrides):
    """槽位齐全，却仍然自报缺少收款人、并且要求澄清。"""
    base = dict(
        scene='scheduled_transfer', operation='create_scheduled_transfer',
        recipient='张三', account_handle='张三', amount='200', day_of_month=15,
        confidence=0.95, write_intent=True, output='confirmation',
        read_tools=['recipients', 'account'], missing_information=['收款人'],
    )
    return Understanding(**{**base, **overrides})


async def test_buildable_request_is_never_turned_back_into_a_question(client, monkeypatch):
    """规划器能建成就出确认卡，哪怕模型坚持说缺收款人。"""
    async def understand(*args):
        return over_reporting()
    monkeypatch.setattr(model, 'understand', understand)

    result = await say(client, '给张三每月15号转200元')

    assert result['type'] == 'confirmation', result
    assert result['engine'] == 'write'
    assert '张三' in str(result['detail'])
    # 关键：不能再出现"还需要……"这种反问
    assert '还需要' not in str(result.get('message', ''))
    assert result.get('awaiting') is None


async def test_a_claim_without_the_slots_still_asks(client, monkeypatch):
    """反过来必须守住：模型说缺，但槽位真的没有时，仍然要问，不能硬编一个收款人。"""
    async def understand(*args):
        return Understanding(
            scene='transfer', operation='transfer', amount='200', write_intent=True,
            read_tools=['recipients'], confidence=0.95, output='confirmation',
        )
    monkeypatch.setattr(model, 'understand', understand)

    result = await say(client, '转200元')

    assert result['type'] == 'message'
    assert result['engine'] == 'clarify'
    # 反问必须可回答：plan_builder 把用户真实的登记人列出来，而不是只说"缺少收款人"
    assert '要转给谁' in result['message']
    assert '张三' in result['message']
    assert 'action_id' not in result


async def test_the_planner_decides_not_the_output_hint(client, monkeypatch):
    """output="clarify" 同样不能推翻一个建得成的计划。"""
    async def understand(*args):
        return over_reporting(missing_information=[], output='clarify')
    monkeypatch.setattr(model, 'understand', understand)

    result = await say(client, '给张三每月15号转200元')

    assert result['type'] == 'confirmation'
    assert result['engine'] == 'write'


async def test_asking_never_moves_money(client, monkeypatch, db):
    """被判定为"办不成"的那一轮，不能留下任何已创建的操作或流水。"""
    async def understand(*args):
        return Understanding(
            scene='transfer', operation='transfer', amount='200', write_intent=True,
            read_tools=['recipients'], confidence=0.95,
        )
    monkeypatch.setattr(model, 'understand', understand)

    await say(client, '转200元')

    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(DemoAction)) == 0
        assert await session.scalar(select(func.count()).select_from(Transaction)) == 0


async def test_delegating_to_write_creates_exactly_one_plan(client, monkeypatch, db):
    """交给 write_node 重新算一次，只能产生一张卡，不能因为算两遍就多出一个操作。"""
    async def understand(*args):
        return over_reporting()
    monkeypatch.setattr(model, 'understand', understand)

    result = await say(client, '给张三每月15号转200元')

    async with db() as session:
        pending = (await session.scalars(
            select(DemoAction).where(DemoAction.status == 'PENDING')
        )).all()
    assert len(pending) == 1
    assert pending[0].id == result['action_id']


@pytest.mark.parametrize('claim', [
    ['收款人'],          # 模型点了槽位的名
    ['还需要收款人'],     # 带前缀的自由文本
    ['收款人信息不明确'],
])
async def test_free_text_claims_cannot_forge_a_gap(client, monkeypatch, claim):
    """缺口是自由文本，所以不能靠"看起来像槽位名"就放行——一律交给规划器裁决。"""
    async def understand(*args):
        return over_reporting(missing_information=claim)
    monkeypatch.setattr(model, 'understand', understand)

    result = await say(client, '给张三每月15号转200元')

    assert result['type'] == 'confirmation', claim
    assert result['engine'] == 'write', claim
