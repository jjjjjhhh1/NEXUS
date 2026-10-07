"""公网暴露前必须成立的那些不变量。

这些不是"密码学最佳实践"清单，是几个具体的、曾经为真的失败模式：

1. 沙箱里 ``POST /api/session`` 给任何人都发一枚会话 Cookie——直接上公网
   就等于把大门拆了。登录界面画在前面，拦不住任何一个直接打 API 的人。
2. 演示口令在会话建立时下发给浏览器。本地是为了让二次核验能被看见；公网上
   这是把执行权限的第二因子挂在门口。
3. 失败提示分得出"用户不存在"和"密码错"，就是一台账号枚举机。
4. 限流按会话计数，而会话可以无限自助申请——限流挡不住任何自动化调用。
5. bcrypt 超过 72 字节静默截断。用户以为设了 200 位口令，实际强度由前 72 字节决定。
"""
import pytest

from nexus.backend.core import auth
from nexus.backend.core.models import OperatorAccount


# ---------------------------------------------------------------------------
# 口令哈希
# ---------------------------------------------------------------------------

def test_password_is_never_stored_in_the_clear():
    digest = auth.hash_password("a-perfectly-fine-passphrase")
    assert digest.startswith("$2b$")
    assert "a-perfectly-fine" not in digest


def test_each_hash_is_salted():
    """同一口令两次哈希必须不同，否则彩虹表直接命中整个账号表。"""
    a = auth.hash_password("same-passphrase-here")
    b = auth.hash_password("same-passphrase-here")
    assert a != b
    assert auth.verify_password("same-passphrase-here", a)
    assert auth.verify_password("same-passphrase-here", b)


def test_a_long_password_is_refused_rather_than_truncated():
    """超过 72 字节必须报错，不能悄悄只哈希前 72 字节。

    静默截断是最坏的一种：用户以为自己设了一个 200 字符的强口令，实际强度
    和 72 字符完全一样，而且永远不会知道。
    """
    with pytest.raises(auth.PasswordRejected):
        auth.hash_password("x" * (auth.MAX_PASSWORD_BYTES + 1))
    # 边界值本身必须可用
    assert auth.hash_password("y" * auth.MAX_PASSWORD_BYTES)


def test_short_passwords_are_refused():
    with pytest.raises(auth.PasswordRejected):
        auth.hash_password("short")


def test_wrong_password_never_verifies():
    digest = auth.hash_password("correct-horse-battery-staple")
    assert not auth.verify_password("correct-horse-battery-stapl", digest)
    assert not auth.verify_password("", digest)


def test_unknown_account_costs_the_same_as_a_wrong_password():
    """没有这条，响应时间就变成了账号枚举器。

    不存在的账号也必须跑一次完整的 bcrypt 校验（对诱饵哈希），否则它会比
    真实账号快一个数量级。
    """
    digest = auth.hash_password("correct-horse-battery-staple")
    assert not auth.verify_password("anything-at-all", None)
    assert not auth.verify_password("anything-at-all", "")
    assert auth.verify_password("correct-horse-battery-staple", digest)


def test_a_corrupt_stored_hash_is_a_failure_not_a_crash():
    """数据库里被写坏的口令哈希不能让登录端点 500。"""
    assert not auth.verify_password("anything", "not-a-bcrypt-hash")
    assert not auth.verify_password("anything", "")


# ---------------------------------------------------------------------------
# 会话令牌
# ---------------------------------------------------------------------------

def test_session_tokens_are_unpredictable_and_stored_as_fingerprints():
    tokens = {auth.new_session_token() for _ in range(200)}
    assert len(tokens) == 200, "令牌必须每次都不同"
    token = auth.new_session_token()
    fingerprint = auth.token_fingerprint(token)
    assert token not in fingerprint
    assert len(fingerprint) == 64
    assert auth.token_fingerprint(token) == fingerprint


def test_session_tokens_have_enough_entropy():
    """32 字节随机性。低于 32 字节的令牌在有其他泄露面时是可以被撞出来的。"""
    token = auth.new_session_token()
    assert len(token) >= 43  # urlsafe base64 of 32 bytes


# ---------------------------------------------------------------------------
# 爆破锁定
# ---------------------------------------------------------------------------

def test_repeated_failures_lock_the_account():
    guard = auth.LoginGuard()
    for _ in range(auth.ACCOUNT_ATTEMPTS - 1):
        assert guard.record_failure("1.2.3.4", "judge") == 0
    assert guard.record_failure("1.2.3.4", "judge") > 0
    assert guard.check("1.2.3.4", "judge") > 0


def test_lockout_is_per_account_not_global():
    """锁一个账号不能把其他人也挡在门外。"""
    guard = auth.LoginGuard()
    for _ in range(auth.ACCOUNT_ATTEMPTS):
        guard.record_failure("1.2.3.4", "judge")
    assert guard.check("1.2.3.4", "judge") > 0
    assert guard.check("1.2.3.4", "someone-else") == 0


def test_a_spray_across_many_addresses_still_locks_the_account():
    """凭据填充的经典打法：换一个 IP 再试一轮。

    只按 IP 计数时，这个打法永远不会触发任何锁定；按账号计数才拦得住。
    """
    guard = auth.LoginGuard()
    for index in range(auth.ACCOUNT_ATTEMPTS):
        guard.record_failure(f"10.0.0.{index}", "judge")
    assert guard.check("10.0.0.9", "judge") > 0, "换 IP 刷同一个账号必须仍然锁住"


def test_an_address_spreading_across_accounts_gets_throttled():
    """反向打法：同一个 IP 轮着试一堆账号。只按账号计数时这是免费的。"""
    guard = auth.LoginGuard()
    locked = False
    for index in range(auth.LOGIN_ATTEMPTS_PER_WINDOW + 2):
        if guard.record_failure("9.9.9.9", f"user-{index}"):
            locked = True
    assert locked, "一个来源试了太多账号必须被限速"
    assert guard.check("9.9.9.9", "brand-new-user") > 0


def test_a_successful_login_clears_the_counters():
    guard = auth.LoginGuard()
    guard.record_failure("1.2.3.4", "judge")
    guard.record_success("1.2.3.4", "judge")
    assert guard.check("1.2.3.4", "judge") == 0


def test_lockout_grows_with_each_round():
    """锁过一次之后再来一轮，锁得更久——上限 1 小时。

    固定时长的锁定挡不住一个愿意等的人：等 5 分钟再继续试，成本低到可以忽略。
    """
    guard = auth.LoginGuard()
    for index in range(auth.ACCOUNT_ATTEMPTS):
        guard.record_failure(f"10.0.0.{index}", "a")
    first = guard.check("10.0.0.0", "a")
    assert first > 0

    # 等待窗口之外换一批来源再来，会再锁一次，而且更久
    guard.locked_until.clear()
    for index in range(auth.ACCOUNT_ATTEMPTS):
        guard.record_failure(f"10.1.0.{index}", "a")
    second = guard.check("10.1.0.0", "a")
    assert second > first, f"第二轮锁定应更长：{second} <= {first}"
    assert second <= auth.LOCK_MAX_SECONDS


def test_failures_outside_the_window_forget():
    """旧失败不能永久累积，否则正常用户迟早被自己锁死。"""
    guard = auth.LoginGuard()
    guard.by_account["judge"] = [0.0]  # 很久以前
    guard.by_address["1.2.3.4"] = [0.0]
    assert guard.record_failure("1.2.3.4", "judge") == 0


# ---------------------------------------------------------------------------
# 端到端：门禁在真实 HTTP 层生效
# ---------------------------------------------------------------------------

async def test_every_business_endpoint_is_closed_to_strangers(client, monkeypatch):
    """未登录时，一个业务接口都不能返回数据。

    逐个点是因为鉴权最容易漏在某一个路由上——漏一个，那一个就是公开的。
    """
    from nexus.backend.core.config import settings
    monkeypatch.setattr(settings, "public_demo", True)

    for path in ("/api/overview", "/api/scheduled-transfers", "/api/memory",
                 "/api/ratings", "/api/audit-chain", "/api/financial-profile"):
        response = await client.get(path)
        assert response.status_code == 401, f"{path} 没有拦住未登录请求"
        assert response.json()["code"] == "UNAUTHENTICATED"


async def test_the_old_door_is_closed(client, monkeypatch):
    """POST /api/session 曾经给任何人都发会话。

    登录界面画在它前面是不够的——任何人绕过界面直接打这个接口就行。
    """
    from nexus.backend.core.config import settings
    monkeypatch.setattr(settings, "public_demo", True)

    response = await client.post("/api/session", json={})
    assert response.status_code == 401


async def test_the_demo_passcode_is_never_disclosed_on_a_public_address(client, monkeypatch):
    """公网模式下二次核验口令绝不能出现在任何响应里。

    演示口令是执行资金操作的第二因子。下发它等于把这一步核验变成走形式。
    """
    from nexus.backend.core.config import settings
    from nexus.backend.core import database
    monkeypatch.setattr(settings, "public_demo", True)

    async def enroll():
        async with database.session_scope() as session:
            session.add(OperatorAccount(
                username="judge", display_name="评审",
                password_hash=auth.hash_password("operator-passphrase-1"),
                is_active=True,
            ))
    await enroll()

    response = await client.post("/api/auth/login", json={
        "username": "judge", "password": "operator-passphrase-1"})
    assert response.status_code == 200, response.text
    body = (await client.post("/api/session", json={})).text
    assert "demo_passcode" not in body, "公网模式不能下发演示口令"


async def test_login_failures_are_indistinguishable(client, monkeypatch):
    from nexus.backend.core.config import settings
    from nexus.backend.core import database
    monkeypatch.setattr(settings, "public_demo", True)
    monkeypatch.setattr(auth, "login_guard", auth.LoginGuard())

    async def enroll():
        async with database.session_scope() as session:
            session.add(OperatorAccount(
                username="judge", display_name="评审",
                password_hash=auth.hash_password("operator-passphrase-1"),
                is_active=True,
            ))
    await enroll()

    unknown = await client.post("/api/auth/login", json={
        "username": "nobody", "password": "whatever-passphrase"})
    wrong = await client.post("/api/auth/login", json={
        "username": "judge", "password": "whatever-passphrase"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["message"] == wrong.json()["message"], \
        "错误提示分得出账号存不存在，就是一台账号枚举机"


async def test_a_correct_login_yields_a_hardened_cookie(client, monkeypatch):
    from nexus.backend.core.config import settings
    from nexus.backend.core import database
    monkeypatch.setattr(settings, "public_demo", True)
    monkeypatch.setattr(settings, "session_cookie_secure", True)
    monkeypatch.setattr(auth, "login_guard", auth.LoginGuard())

    async def enroll():
        async with database.session_scope() as session:
            session.add(OperatorAccount(
                username="judge", display_name="评审",
                password_hash=auth.hash_password("operator-passphrase-1"),
                is_active=True,
            ))
    await enroll()

    response = await client.post("/api/auth/login", json={
        "username": "judge", "password": "operator-passphrase-1"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie, "HttpOnly 缺失意味着 XSS 能直接读走会话"
    assert "Secure" in cookie, "Secure 缺失意味着会话令牌在明文链路上裸奔"
    assert "SameSite=strict" in cookie, "SameSite 缺失意味着 CSRF 有了支点"


async def test_logout_kills_the_session_server_side(client, monkeypatch):
    """只清浏览器 Cookie 是不够的——令牌还在库里，换个浏览器照样能用。"""
    from nexus.backend.core.config import settings
    from nexus.backend.core import database
    monkeypatch.setattr(settings, "public_demo", True)
    monkeypatch.setattr(settings, "session_cookie_secure", False)
    monkeypatch.setattr(auth, "login_guard", auth.LoginGuard())

    async def enroll():
        async with database.session_scope() as session:
            session.add(OperatorAccount(
                username="judge", display_name="评审",
                password_hash=auth.hash_password("operator-passphrase-1"),
                is_active=True,
            ))
    await enroll()

    await client.post("/api/auth/login", json={
        "username": "judge", "password": "operator-passphrase-1"})
    token = client.cookies.get("nexus_session")
    assert token

    assert (await client.post("/api/auth/logout", json={})).status_code == 200

    replay = await client.get("/api/overview", headers={"Cookie": f"nexus_session={token}"})
    assert replay.status_code == 401, "登出后令牌必须失效，不能重放"


async def test_logout_works_after_the_session_has_done_something(client, monkeypatch):
    """登出必须在一个"干过活"的会话上照样成功。

    曾经的实现是删除会话行。会话一旦执行过转账或产生过对话，就有记录外键指向
    它，删除触发外键冲突 → 事务回滚 → 令牌没被销毁：接口返回 500，用户看着像
    登出失败，实际上令牌还能继续用。测试里之前登出的都是"没干过任何事"的空
    会话，所以一直没暴露。
    """
    from uuid import uuid4
    from nexus.backend.core.config import settings
    from nexus.backend.core import database
    monkeypatch.setattr(settings, "public_demo", True)
    monkeypatch.setattr(settings, "session_cookie_secure", False)
    monkeypatch.setattr(auth, "login_guard", auth.LoginGuard())

    async def enroll():
        async with database.session_scope() as session:
            session.add(OperatorAccount(
                username="judge", display_name="评审",
                password_hash=auth.hash_password("operator-passphrase-1"),
                is_active=True,
            ))
    await enroll()

    await client.post("/api/auth/login", json={
        "username": "judge", "password": "operator-passphrase-1"})
    token = client.cookies.get("nexus_session")

    # 让这个会话真的产生历史：一次对话 + 一次确认卡
    await client.post("/api/messages", json={
        "message": "现在给张三转50元", "request_id": str(uuid4())})

    logout = await client.post("/api/auth/logout", json={})
    assert logout.status_code == 200, f"有历史记录的会话登出失败：{logout.text}"

    replay = await client.get("/api/overview", headers={"Cookie": f"nexus_session={token}"})
    assert replay.status_code == 401, "登出后令牌必须立刻失效"


async def test_brute_forcing_locks_the_account(client, monkeypatch):
    from nexus.backend.core.config import settings
    from nexus.backend.core import database
    monkeypatch.setattr(settings, "public_demo", True)
    monkeypatch.setattr(auth, "login_guard", auth.LoginGuard())

    async def enroll():
        async with database.session_scope() as session:
            session.add(OperatorAccount(
                username="judge", display_name="评审",
                password_hash=auth.hash_password("operator-passphrase-1"),
                is_active=True,
            ))
    await enroll()

    saw_lock = False
    for index in range(auth.ACCOUNT_ATTEMPTS + 3):
        response = await client.post("/api/auth/login", json={
            "username": "judge", "password": f"guess-number-{index}"})
        if response.status_code == 429:
            saw_lock = True
    assert saw_lock, "连续失败必须触发锁定"


async def test_a_disabled_account_cannot_log_in(client, monkeypatch):
    from nexus.backend.core.config import settings
    from nexus.backend.core import database
    monkeypatch.setattr(settings, "public_demo", True)
    monkeypatch.setattr(auth, "login_guard", auth.LoginGuard())

    async def enroll():
        async with database.session_scope() as session:
            session.add(OperatorAccount(
                username="retired", display_name="已停用",
                password_hash=auth.hash_password("operator-passphrase-1"),
                is_active=False,
            ))
    await enroll()

    response = await client.post("/api/auth/login", json={
        "username": "retired", "password": "operator-passphrase-1"})
    assert response.status_code == 401


async def test_local_development_needs_no_login(client):
    """本地沙箱不能被门禁挡住——这是开发时唯一的调试路径。"""
    state = await client.get("/api/auth/state")
    assert state.json()["required"] is False
    assert (await client.get("/api/overview")).status_code == 200


async def test_health_does_not_advertise_the_model_vendor(client, monkeypatch):
    """公开的健康检查不该告诉扫描器你接了哪家模型、叫什么模型。"""
    from nexus.backend.core.config import settings
    monkeypatch.setattr(settings, "public_demo", True)
    body = (await client.get("/api/health")).json()
    assert "model" not in body
    assert body["mode"] == "public-demo"
