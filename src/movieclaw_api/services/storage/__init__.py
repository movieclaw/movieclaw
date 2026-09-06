"""运行期数据目录的登记表与缓存管理（docs/design/cache-management.md）。

- registry.py：data/ 下每个目录的唯一事实源——用途、清理策略、孤儿/占用探测；
- service.py：按登记表统计占用、执行清理，是「设置 → 更新与维护 → 缓存管理」的后端。
"""
