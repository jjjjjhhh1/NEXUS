"""Card questions must use linked-account balances rather than aggregate funds."""
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from nexus.backend.agent import model
from nexus.backend.agent.read_views import card_balance_view
from nexus.backend.agent.toolkit import get_cards
from nexus.backend.agent.understanding import Understanding
from nexus.backend.core.models import Account, Card, User


async def test_card_question_returns_shared_balance_and_limits(client, monkeypatch):
    async def understand(message, context=None):
        return Understanding(scene="cross_scene", read_tools=["account"], confidence=0.99,
                             write_intent=False, output="text")
    monkeypatch.setattr(model, "understand", understand)
    response = await client.post('/api/messages', json={
        'message': '日常卡和备用卡分别多少钱', 'request_id': str(uuid4())})
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer['type'] == 'card_balances'
    assert [c['last4'] for c in answer['cards']] == ['8826', '1024']
    assert all(c['available'] == '¥28,650.00' for c in answer['cards'])
    assert all(c['shared_account'] for c in answer['cards'])
    assert '不能重复相加' in answer['summary']
    assert answer['cards'][0]['single_limit'] == '¥5,000.00'
    assert 'hero' not in answer  # no all-account total as a substitute
    overview = (await client.get('/api/overview')).json()
    assert all(c['shared_account'] for c in overview['cards'])
    assert all(c['available'] == '28650.00' for c in overview['cards'])


async def test_card_views_scope_balance_to_owner_and_selected_card(db, seeded):
    async with db() as session:
        card = await session.get(Card, seeded['card'])
        card.bank_name = 'Nexus 日常卡'
        reserve = Account(user_id=seeded['user'], type='savings', balance=Decimal('250'),
                          available_balance=Decimal('250'))
        session.add(reserve)
        await session.flush()
        session.add_all([
            Card(account_id=reserve.id, bank_name='Nexus 备用卡', last4='5678', card_type='DEBIT'),
            Card(account_id=seeded['destination'], bank_name='Nexus 私人卡', last4='9999', card_type='DEBIT'),
        ])
    async with db() as session:
        answer = await card_balance_view(session, seeded['user'], '日常卡和备用卡各有多少余额')
        assert [c['available'] for c in answer['cards']] == ['¥1,000.00', '¥250.00']
        assert not any(c['shared_account'] for c in answer['cards'])
        one = await card_balance_view(session, seeded['user'], '尾号5678卡里有多少钱')
        assert len(one['cards']) == 1 and one['cards'][0]['available'] == '¥250.00'
        unknown = await card_balance_view(session, seeded['user'], '私人卡多少钱')
        assert unknown['engine'] == 'clarify' and 'cards' not in unknown
    tool = await get_cards(seeded['user'])
    assert {c['available'] for c in tool['cards']} == {'¥1,000.00', '¥250.00'}
    assert all(c['last4'] != '9999' for c in tool['cards'])
    assert '限额不是余额' in tool['balance_note']
