"""
Nexus 配置管理
统一管理所有配置，支持多环境（development / test / production / demo）
"""
import os
from typing import Literal
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr, AliasChoices


class Settings(BaseSettings):
    """全局配置"""

    # ============ 应用基础 ============
    app_name: str = "Nexus Banking Agent"
    app_version: str = "1.0.0"
    environment: Literal["development", "test", "production", "demo"] = "development"
    debug: bool = True

    # ============ 数据库 ============
    database_url: str = Field(
        default="sqlite+aiosqlite:///./nexus-demo.db",
        description="数据库连接 URL"
    )
    database_echo: bool = False

    # ============ Redis / 缓存 ============
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl: int = 300  # 默认缓存 5 分钟

    # ============ JWT / 认证 ============
    jwt_secret: str = "nexus-banking-dev-secret-please-change"
    jwt_expire_minutes: int = 60 * 24  # 24 小时

    # ============ LLM ============
    llm_provider: Literal["deepseek", "openai", "mock"] = "deepseek"
    llm_enabled: bool = False
    llm_api_key: SecretStr = Field(default=SecretStr(""), validation_alias=AliasChoices("NEXUS_LLM_API_KEY", "LLM_API_KEY"))
    llm_base_url: str = Field(default="https://api.deepseek.com/v1", validation_alias=AliasChoices("NEXUS_LLM_BASE_URL", "LLM_BASE_URL"))
    llm_model: str = Field(default="deepseek-chat", validation_alias=AliasChoices("NEXUS_LLM_MODEL", "LLM_MODEL"))
    llm_timeout: int = 30  # 秒
    llm_max_retries: int = 2

    # ============ Agent 循环 ============
    agent_loop: bool = Field(default=False, validation_alias=AliasChoices("NEXUS_AGENT_LOOP"), description="启用 Tool+AgentExecutor 工具循环作为 LLM 分支主路径；关闭则用闭包 schema 意图解析")

    # ============ 风险引擎 ============
    risk_transfer_baseline_multiplier: float = 3.0  # 超基线 3 倍触发
    risk_high_amount_threshold: float = 10000  # 1 万以上高额
    risk_night_hours: tuple = (23, 6)  # 23:00 - 06:00 视为夜间

    # ============ 调度 ============
    scheduler_enabled: bool = True
    scheduler_timezone: str = "Asia/Shanghai"

    memory_enabled: bool = True
    memory_window_chars: int = Field(default=2400, ge=500, le=8000)
    memory_window_turns: int = Field(default=8, ge=2, le=20)
    memory_fact_days: int = Field(default=180, ge=1, le=365)
    audit_directory: str = str(Path(__file__).resolve().parents[2] / ".runtime" / "audit")

    # ============ 第三方 API（可选）============
    baidu_ocr_api_key: str = ""
    baidu_ocr_secret_key: str = ""
    amap_api_key: str = ""
    kuaidi100_api_key: str = ""
    wechat_app_id: str = ""
    wechat_app_secret: str = ""
    feishu_app_id: str = ""
    feishu_app_secret: str = ""

    # ============ 演示模式 ============
    demo_mode: bool = True
    # ============ 公网演示部署 ============
    # 打开后：服务允许绑定非 localhost 的地址，入口从"本地沙箱"切换为"受控公网演示"。
    #
    # 这个开关**不是**把服务变成生产银行系统：业务数据仍是演示数据，写操作仍然
    # 全部走确认卡 + 二次核验，没有真实资金通道。它只回答一个问题：这个进程是否
    # 会对陌生流量开放。默认关闭，本地开发不需要设置。
    #
    # 打开它的同时必须具备（启动时会检查，不满足直接拒绝启动）：
    #   1. 至少存在一个 operator 账号；
    #   2. 配置了 allowed_hosts；
    #   3. 配置了 session_cookie_secure（公网必须为 true）。
    public_demo: bool = Field(default=False, validation_alias=AliasChoices("NEXUS_PUBLIC_DEMO"))

    # 反代后应用看到的 Host。留空表示不额外限制（不推荐）。
    # Nginx 必须透传原始 Host，否则这里要写上公网域名或 IP。
    allowed_hosts: str = Field(
        default="",
        validation_alias=AliasChoices("NEXUS_ALLOWED_HOSTS"),
        description="逗号分隔，例如 59.110.23.216,demo.example.com",
    )

    # 公网部署必须为 true：会话 Cookie 只走 HTTPS。
    # 放在这里由应用强制，而不是依赖部署者记得给 Nginx 配 HSTS。
    session_cookie_secure: bool = Field(
        default=False, validation_alias=AliasChoices("NEXUS_SESSION_COOKIE_SECURE")
    )

    # 登录会话有效期。演示场景用一天足够，生产系统应该更短。
    session_max_age_seconds: int = Field(
        default=86400, validation_alias=AliasChoices("NEXUS_SESSION_MAX_AGE"), ge=300, le=604800
    )

    # 公网上要不要照常下发演示口令（即二次核验用的 4 位码）。
    #
    # 默认 false：演示口令是执行资金操作的第二因子，下发给任何登进来的人，
    # 这一步核验就成了走过场。关掉之后首次写操作会提示用户自己设一个 4 位码，
    # 设完就和本地完全一致——多一步，仅此而已。
    #
    # 如果你的场景是"只发给几位可信的评委、看一眼就完事"，把它设成 true，
    # 公网行为就和本地一模一样。风险由部署者自己承担。
    demo_passcode_on_public: bool = Field(
        default=False, validation_alias=AliasChoices("NEXUS_DEMO_PASSCODE_PUBLIC")
    )

    @property
    def allowed_host_list(self) -> list[str]:
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]

    def public_demo_ready(self) -> tuple[bool, list[str]]:
        """Refuse to serve strangers unless the operator has actually set it up.

        Failing at startup is the point. A public address reached with the
        sandbox defaults still open is exactly the accident this prevents, and
        a loud refusal is cheaper than discovering it from someone else's
        browser history.
        """
        if not self.public_demo:
            return True, []
        problems: list[str] = []
        if not self.allowed_host_list:
            problems.append("NEXUS_ALLOWED_HOSTS 未设置，公网部署必须显式声明允许的 Host")
        if not self.session_cookie_secure:
            problems.append("NEXUS_SESSION_COOKIE_SECURE 必须为 true，否则会话 Cookie 会在明文链路上发送")
        return not problems, problems
    demo_default_user: str = "demo_user_01"
    demo_time_acceleration: bool = True

    # ============ 日志 ============
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "text"

    model_config = SettingsConfigDict(
        env_file=(".env", str(Path(__file__).resolve().parents[2] / ".env")),
        env_prefix="NEXUS_", case_sensitive=False, extra="ignore"
    )


# 单例
settings = Settings()


def is_demo() -> bool:
    """是否演示模式"""
    return settings.demo_mode


def is_production() -> bool:
    """是否生产环境"""
    return settings.environment == "production"
