"""收款人槽位的"已填"判定必须在所有地方是同一个定义。

模型可以把"妈妈"放进 recipient（登记名），也可以放进 account_handle（用户称呼），
plan_builder._resolve_recipient 两种都认。槽位完整性检查如果只认 recipient，
用户明明说了"给妈妈"，agent 还是会追问一次收款人——实测 12 次里有 6 次。
这不是模型的抖动，是三处定义不一致。
"""
import pytest

from nexus.backend.agent.routing import missing_write_slots, payee_slot_filled, resolve_operation
from nexus.backend.agent.understanding import Understanding


def understanding(**overrides) -> Understanding:
    base = dict(scene="scheduled_transfer", operation="create_scheduled_transfer",
                amount="200", day_of_month=15, confidence=0.95, write_intent=True)
    return Understanding(**{**base, **overrides})


def test_a_handle_alone_satisfies_the_payee_slot():
    """用户说"我妈"而登记名是"妈妈"：这是已给出的信息，不该被当成缺失。"""
    item = understanding(recipient=None, account_handle="我妈")
    assert payee_slot_filled(item.model_dump()) is True
    assert missing_write_slots(item) == []


def test_a_registered_name_alone_satisfies_the_payee_slot():
    assert missing_write_slots(understanding(recipient="妈妈", account_handle=None)) == []


def test_both_forms_present_is_still_fine():
    assert missing_write_slots(understanding(recipient="妈妈", account_handle="我妈")) == []


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_genuinely_missing_payee_still_asks(blank):
    """修好"误报缺失"的同时不能把"真的没给"也一起放过去。"""
    item = understanding(recipient=blank, account_handle=blank)
    assert missing_write_slots(item) == ["收款人"]


def test_a_blank_handle_does_not_count_as_filled():
    """空白字符串不是信息。model_dump 出来是 None，但历史回写可能带来空串。"""
    assert payee_slot_filled({"recipient": None, "account_handle": "  "}) is False


def test_completeness_still_reports_other_missing_slots():
    """收款人已给但金额没给时，必须点名金额，不能因为修了一个槽就整体放行。"""
    item = understanding(recipient=None, account_handle="我妈", amount=None)
    assert missing_write_slots(item) == ["金额"]


def test_slot_check_and_resolver_agree_on_what_counts_as_a_payee():
    """完整性检查和真正的解析器必须共享同一个定义，否则还会再次分叉。"""
    from nexus.backend.agent.plan_builder import _resolve_recipient  # noqa: F401  (import proves wiring)
    for filled in ({"recipient": "妈妈"}, {"account_handle": "我妈"},
                   {"recipient": "妈妈", "account_handle": "我妈"}):
        assert payee_slot_filled(filled) is True
    assert resolve_operation(understanding()) == "create_scheduled_transfer"
