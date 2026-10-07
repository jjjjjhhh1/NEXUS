"""
调度服务
基于 APScheduler
负责：
- 生日计划触发
- 预约转账执行
- 周期扣费检测
- 演示模式时间推进
"""
from datetime import datetime, timedelta
from typing import Callable, Optional, Any
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR

from ..core.logging import logger
from ..core.config import settings


class SchedulerService:
    """调度服务"""

    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)
        self._listeners_registered = False
        self._demo_time_offset = timedelta(0)  # 演示模式时间偏移

    def start(self) -> None:
        """启动调度器"""
        if not self._listeners_registered:
            self._add_listeners()
            self._listeners_registered = True
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("调度器已启动")

    def shutdown(self) -> None:
        """关闭调度器"""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("调度器已关闭")

    def _add_listeners(self) -> None:
        """添加事件监听器"""

        def job_executed(event):
            logger.info(f"[JOB OK] {event.job_id}")

        def job_error(event):
            logger.error(f"[JOB ERR] {event.job_id}: {event.exception}")

        self.scheduler.add_listener(job_executed, EVENT_JOB_EXECUTED)
        self.scheduler.add_listener(job_error, EVENT_JOB_ERROR)

    # ============ 一次性任务 ============
    def schedule_once(
        self,
        func: Callable,
        run_at: datetime,
        job_id: Optional[str] = None,
        args: tuple = (),
        kwargs: Optional[dict] = None,
    ) -> str:
        """安排一次性任务

        演示模式下，如果 run_at 在 demo time 之前，会立即触发
        """
        kwargs = kwargs or {}

        # 演示模式：把目标时间调整为 demo time
        actual_time = self._apply_demo_offset(run_at)

        if actual_time < datetime.now(actual_time.tzinfo):
            # 已经过期，立即执行
            actual_time = datetime.now(actual_time.tzinfo) + timedelta(seconds=1)

        if job_id is None:
            job_id = f"once_{func.__name__}_{run_at.timestamp()}"

        self.scheduler.add_job(
            func,
            DateTrigger(run_date=actual_time),
            id=job_id,
            args=args,
            kwargs=kwargs,
            replace_existing=True,
        )
        logger.info(f"已安排任务 {job_id} @ {actual_time}")
        return job_id

    # ============ 周期任务 ============
    def schedule_interval(
        self,
        func: Callable,
        seconds: int,
        job_id: Optional[str] = None,
        args: tuple = (),
        kwargs: Optional[dict] = None,
    ) -> str:
        """安排周期任务"""
        kwargs = kwargs or {}
        if job_id is None:
            job_id = f"interval_{func.__name__}_{seconds}s"

        self.scheduler.add_job(
            func,
            IntervalTrigger(seconds=seconds),
            id=job_id,
            args=args,
            kwargs=kwargs,
            replace_existing=True,
        )
        return job_id

    # ============ Cron 任务 ============
    def schedule_cron(
        self,
        func: Callable,
        hour: int,
        minute: int = 0,
        job_id: Optional[str] = None,
        args: tuple = (),
        kwargs: Optional[dict] = None,
    ) -> str:
        """按小时 / 分钟定时"""
        kwargs = kwargs or {}
        if job_id is None:
            job_id = f"cron_{func.__name__}_{hour}:{minute}"

        self.scheduler.add_job(
            func,
            CronTrigger(hour=hour, minute=minute),
            id=job_id,
            args=args,
            kwargs=kwargs,
            replace_existing=True,
        )
        return job_id

    # ============ 任务管理 ============
    def cancel_job(self, job_id: str) -> bool:
        """取消任务"""
        try:
            self.scheduler.remove_job(job_id)
            logger.info(f"已取消任务 {job_id}")
            return True
        except Exception as e:
            logger.warning(f"取消任务 {job_id} 失败: {e}")
            return False

    def get_jobs(self) -> list[dict]:
        """列出所有任务"""
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "trigger": str(job.trigger),
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            })
        return jobs

    # ============ 演示模式时间推进 ============
    def set_demo_time_offset(self, offset: timedelta) -> None:
        """设置演示模式时间偏移"""
        self._demo_time_offset = offset
        logger.info(f"演示时间偏移已设置为 {offset}")

    def advance_demo_time(self, target_time: datetime) -> None:
        """推进演示时间到指定时间"""
        delta = target_time - datetime.now()
        self._demo_time_offset = delta
        logger.info(f"演示时间已推进到 {target_time}，偏移 {delta}")

    def _apply_demo_offset(self, dt: datetime) -> datetime:
        """应用演示时间偏移"""
        if not settings.demo_time_acceleration:
            return dt
        return dt + self._demo_time_offset

    def now(self) -> datetime:
        """获取当前演示时间"""
        if not settings.demo_time_acceleration:
            return datetime.now()
        return datetime.now() + self._demo_time_offset

    # ============ 业务专用：生日计划触发 ============
    def schedule_plan_order(
        self,
        plan_id: int,
        order_callback: Callable,
        trigger_at: datetime,
    ) -> str:
        """安排计划订单触发"""
        return self.schedule_once(
            order_callback,
            trigger_at,
            job_id=f"plan_order_{plan_id}",
            kwargs={"plan_id": plan_id},
        )

    # ============ 业务专用：预约转账 ============
    def schedule_transfer(
        self,
        tx_id: int,
        transfer_callback: Callable,
        trigger_at: datetime,
    ) -> str:
        """安排预约转账"""
        return self.schedule_once(
            transfer_callback,
            trigger_at,
            job_id=f"transfer_{tx_id}",
            kwargs={"tx_id": tx_id},
        )

    # ============ 业务专用：周期扣费检测 ============
    def schedule_subscription_check(
        self,
        check_callback: Callable,
    ) -> str:
        """每日检查周期扣费"""
        return self.schedule_cron(
            check_callback,
            hour=9,
            minute=0,
            job_id="daily_subscription_check",
        )


# 单例
scheduler_service = SchedulerService()