"""应用服务配置域（「设置 → 应用设置」页的数据模型）。

当前只有一个字段——外部访问地址（``external_url``）：从网络上能访问到本应用
的完整地址（如 ``http://192.168.1.10:3000`` 或反代后的
``https://movie.example.com``），供生成通知链接、回调地址等绝对 URL 的场景
使用（Agent 系统提示词的环境段也注入它来拼页面链接）。Docker 桥接部署下，
Jellyfin 兼容层的 UDP 自动发现探测到容器内网 IP 时也改用它应答（见
``movieclaw_jellyfin/udp.py``）。

为什么端口不在这个配置域里：对外端口（前端监听口）确实可以在应用内改，但它
的事实源是 data 卷上的端口文件而不是数据库——真正读它的是 entrypoint 与
Docker HEALTHCHECK 两个不读数据库的 shell，且端口绑不上时 entrypoint 要能就地
废弃它回落。读写逻辑见 ``services/app_config.py`` 的「对外端口」一节，解析口径
见 ``docker/resolve-web-port.sh``。后端监听端口（容器内 8000）仍不可应用内配置：
它是 Next 反代目标、构建时固化，源码部署下用 ``APP_PORT`` 环境变量调整。
"""

from __future__ import annotations

from pydantic import Field

from movieclaw_api.settings.base import SettingSchema, register_setting

APP_SERVER_NAMESPACE = "app.server"


@register_setting(namespace=APP_SERVER_NAMESPACE, title="应用设置")
class AppServerSetting(SettingSchema):
    """应用服务配置：外部访问地址。

    历史字段说明：早期版本曾有 ``port``（后端监听端口）字段，后因「改后端
    端口对外部访问没有意义」而移除；基类 ``extra="ignore"`` 保证带旧字段的
    存量记录读取不报错。现在可配的是**对外端口**，且不落库（见模块文档）。
    """

    external_url: str = Field(
        default="",
        description="网络可访问到本应用的完整地址（http/https），供生成绝对链接使用；空 = 未配置",
    )
