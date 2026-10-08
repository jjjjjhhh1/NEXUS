"""A manually changed verification code must invalidate the disclosed demo code."""
from nexus.backend.agent.security import step_up
from nexus.backend.agent.demo_agent import ensure_demo_passcode, step_up_states


def test_changing_passcode_clears_stale_demo_disclosure():
    state=step_up.StepUpState()
    step_up.set_passcode(state,'1234')
    state.demo_passcode='1234'
    step_up.set_passcode(state,'2468')
    assert state.demo_passcode is None
    step_up.check_passcode(state,'2468')


def test_session_reentry_never_returns_old_demo_code_after_manual_change():
    session_id=987654
    ensure_demo_passcode(session_id)
    step_up.set_passcode(step_up_states[session_id],'2468')
    assert ensure_demo_passcode(session_id)==''
    step_up.check_passcode(step_up_states[session_id],'2468')
