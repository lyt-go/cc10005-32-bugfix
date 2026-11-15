"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

跨模块联动（限速恢复需要同时读健康报告、写速率决策和审计记录）依赖 ``locked()``
提供的可重入锁：一次业务动作里的所有读改写都在同一把锁内完成，避免两个窗口并发
恢复时出现一半生效、一半回滚的状态错乱。
"""
from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 联动过程中产生的新表，不放种子数据：速率决策记录与恢复凭证（书签/上传/导入）。
EXTRA_TABLES = ("rate_decision", "recovery_artifact")


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for name in EXTRA_TABLES:
            self._tables.setdefault(name, [])
        self._lock = RLock()

    @contextmanager
    def locked(self) -> Iterator[None]:
        """跨模块原子动作统一走这把锁；可重入，服务之间互相调用也不会死锁。"""
        with self._lock:
            yield

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
