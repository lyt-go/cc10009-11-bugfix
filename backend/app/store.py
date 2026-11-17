"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
业务列表之外的辅助数据（规则快照、去重URL、同步记录）放在 aux 表里，
不计入运营概览的模块列表，但和业务数据共用同一份进程内存储。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._aux_tables: dict[str, list[dict[str, Any]]] = {}

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    # ------------------------------------------------------------ 辅助表

    def aux_rows(self, name: str) -> list[dict[str, Any]]:
        """辅助表：快照、快照URL明细、同步记录等，不参与模块统计。"""
        return self._aux_tables.setdefault(name, [])

    def aux_find(self, name: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.aux_rows(name):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def aux_next_id(self, name: str) -> int:
        return max((int(row.get("id", 0)) for row in self.aux_rows(name)), default=0) + 1

    def aux_names(self) -> list[str]:
        return sorted(self._aux_tables)

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
