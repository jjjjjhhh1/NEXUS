"""
缓存服务
基于内存（开发）/ Redis（生产）
用于反诈基线、汇率、产品库等热点数据
"""
import json
import time
from typing import Any, Optional
from collections import OrderedDict


class CacheEntry:
    """缓存条目"""

    def __init__(self, value: Any, ttl: int):
        self.value = value
        self.created_at = time.time()
        self.ttl = ttl

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl


class CacheService:
    """内存缓存服务（LRU）"""

    def __init__(self, max_size: int = 1000):
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._max_size = max_size

    def get(self, key: str) -> Optional[Any]:
        """获取缓存"""
        if key not in self._cache:
            return None

        entry = self._cache[key]
        if entry.is_expired():
            del self._cache[key]
            return None

        # LRU: 移动到末尾
        self._cache.move_to_end(key)
        return entry.value

    def set(self, key: str, value: Any, ttl: int = 300) -> None:
        """设置缓存"""
        if key in self._cache:
            del self._cache[key]

        # 容量控制
        if len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)

        self._cache[key] = CacheEntry(value, ttl)

    def delete(self, key: str) -> None:
        """删除缓存"""
        if key in self._cache:
            del self._cache[key]

    def clear(self) -> None:
        """清空"""
        self._cache.clear()

    def exists(self, key: str) -> bool:
        """检查存在"""
        return self.get(key) is not None

    def get_or_set(
        self, key: str, factory, ttl: int = 300
    ) -> Any:
        """获取或创建"""
        cached = self.get(key)
        if cached is not None:
            return cached

        value = factory()
        self.set(key, value, ttl)
        return value

    def stats(self) -> dict:
        """缓存统计"""
        expired_count = sum(1 for e in self._cache.values() if e.is_expired())
        return {
            "total": len(self._cache),
            "expired": expired_count,
            "max_size": self._max_size,
        }


# 单例
cache = CacheService()


# ============ 业务缓存键 ============
class CacheKeys:
    """缓存键命名规范"""

    # 反诈
    RISK_BASELINE = "risk:baseline:{user_id}"  # 用户行为基线
    FRAUD_KEYWORDS = "risk:fraud_keywords"  # 反诈话术词库

    # 汇率（每日更新）
    EXCHANGE_RATE = "rate:{source}:{target}"

    # 产品库（每日更新）
    PRODUCT_LIST = "products:all"
    PRODUCT_DETAIL = "product:{id}"

    # 节假日（每年更新）
    HOLIDAYS = "holidays:cn:{year}"

    # 风险事件统计
    RISK_STATS = "risk:stats:{user_id}"


# ============ 业务专用缓存包装 ============
class BusinessCache:
    """业务缓存封装"""

    def __init__(self):
        self.cache = cache

    def get_risk_baseline(self, user_id: int) -> Optional[dict]:
        """获取用户风险基线"""
        return self.cache.get(CacheKeys.RISK_BASELINE.format(user_id=user_id))

    def set_risk_baseline(self, user_id: int, baseline: dict, ttl: int = 600):
        """缓存用户风险基线（10 分钟）"""
        self.cache.set(
            CacheKeys.RISK_BASELINE.format(user_id=user_id),
            baseline,
            ttl=ttl,
        )

    def invalidate_risk_baseline(self, user_id: int):
        """失效用户风险基线（转账后调用）"""
        self.cache.delete(CacheKeys.RISK_BASELINE.format(user_id=user_id))

    def get_exchange_rate(self, from_currency: str, to_currency: str) -> Optional[float]:
        """获取汇率缓存"""
        return self.cache.get(CacheKeys.EXCHANGE_RATE.format(source=from_currency, target=to_currency))

    def set_exchange_rate(self, from_currency: str, to_currency: str, rate: float, ttl: int = 3600 * 12):
        """缓存汇率（12 小时）"""
        self.cache.set(
            CacheKeys.EXCHANGE_RATE.format(source=from_currency, target=to_currency),
            rate,
            ttl=ttl,
        )

    def get_fraud_keywords(self) -> Optional[list]:
        """获取反诈关键词"""
        return self.cache.get(CacheKeys.FRAUD_KEYWORDS)

    def set_fraud_keywords(self, keywords: list, ttl: int = 3600 * 24):
        """缓存反诈关键词（24 小时）"""
        self.cache.set(CacheKeys.FRAUD_KEYWORDS, keywords, ttl=ttl)


business_cache = BusinessCache()
