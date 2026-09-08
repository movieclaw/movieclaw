"""成员级个人数据的公共基座：哨兵列 + 注册表。

## 为什么需要这一层

项目里「跟人走」的表越来越多（观看状态、搜索历史、下载保存位置记忆……），
它们共享同一套约定，此前靠每张表各自抄一遍来维持：

1. ``member_id`` 是 **NOT NULL DEFAULT 0 的整数列，0 表示超管**——超管不在
   ``member`` 表里（见 ``models/member.py`` 顶部注释），用哨兵值才能让超管与
   成员共用一套存储；
2. 它**不是外键**，所以删除成员时必须由服务层显式清理；
3. 刻意不用 NULL 表示超管：SQLite 的 UNIQUE 视 NULL 互不相等，可空列进唯一
   约束会让同一单元插出无限行（``playback_state`` 的注释里有同样的说明）。

第 2 条是本模块存在的真正理由。``delete_member`` 过去逐行手写清理，漏写一张
表的后果不是「残留几行垃圾数据」——SQLite 会复用已删成员的行 id，新成员建号
后将**继承前一个人的数据**，是跨人隐私泄漏。靠「记得加一行」来防这种事，迟早
会漏。改成注册表之后，挂了 ``MemberScopedMixin`` 又登记的表自动被清理覆盖，
而 CI 守卫会拦住「有 member_id 却没登记」的新表。
"""

from __future__ import annotations

from typing import TypeVar

from sqlmodel import Field, SQLModel


class MemberScopedMixin(SQLModel):
    """带成员归属的表都混入本类，拿到统一的 ``member_id`` 哨兵列。

    只提供列本身；唯一约束要不要带上 ``member_id``、带上后的顺序如何，取决于
    各表的查询形态，由各表自己声明（例如 ``playback_state`` 把它放进四列联合
    唯一约束的首位）。
    """

    member_id: int = Field(default=0, index=True, description="归属成员；0=超管（哨兵）")


# 注册表：所有需要「删除成员时一并清理」的模型。用 list 而非 set 保持登记顺序，
# 清理时按登记顺序执行，出问题时日志可读。
_registry: list[type[SQLModel]] = []

T = TypeVar("T", bound=type[SQLModel])


def register_member_scoped(model: T) -> T:
    """把模型登记为「成员级个人数据」，装饰在 ``table=True`` 的类上。

    登记即承诺：删除成员时该表中属于此人的行会被 ``delete_member`` 清空。
    新增成员级表时**只需要加这一行装饰器**，不必再去改 ``delete_member``。
    """
    if not issubclass(model, MemberScopedMixin):
        raise TypeError(f"{model.__name__} 需要先混入 MemberScopedMixin 才能登记为成员级数据")
    if model not in _registry:
        _registry.append(model)
    return model


def member_scoped_models() -> list[type[SQLModel]]:
    """返回全部已登记的成员级模型（删除成员时遍历清理，以及 CI 守卫比对用）。"""
    return list(_registry)
