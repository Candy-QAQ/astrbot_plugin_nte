# 更新记录

## v1.1.2（2026-10-08）

- 修复 v1.1.1 上架审核指出的日志来源问题：`nte.py` 统一通过 `from astrbot.api import logger` 使用 AstrBot 日志。
- 删除内置日志模块、自建日志文件及 root logger 配置，日志级别、输出位置和轮转交由 AstrBot 管理。
- HTTP 日志继续只记录请求方法、域名、路径和状态码；异常通过 AstrBot logger 记录，保留现有响应体和凭据保护。
- 调整离线测试的 AstrBot logger 模拟，并新增日志来源、日志内容及导入时不创建日志文件的回归检查。


## v1.1.1（2026-10-05）

修复 [issue #1](https://github.com/Candy-QAQ/astrbot_plugin_nte/issues/1) 及其中已确认的相关缺陷：

- 等待验证码时只接收 4 至 8 位 ASCII 数字，普通私聊和指令正常放行；登录尝试失败后结束流程并提示重新发起登录。
- 增加 `/ntecancel`，只取消登录；重复输入不会重复登录，取消、注销或重新发起流程后，旧请求不能重新绑定账号或覆盖新会话。
- 验证码按用户和手机号限制为 60 秒冷却、每天各 10 次；失败请求也计数，两种短信登录共用额度。
- 塔吉多域下的登录、刷新、角色、社区签到和游戏签到接口统一添加 `ds` 签名。
- 全部 HTTP 调用设置连接和读取超时；异常移除原始响应体，HTTP 日志只记录请求元数据；日志初始化支持重复调用及换日关闭旧文件。
- KV 读改写加锁；签到结束后仅更新仍存在且凭据未变的绑定，避免注销账号复活、覆盖新登录凭据或丢失其他用户的更新。兼容旧用户键迁移期间的签到。
- 同一账号的手动和自动签到避免并发重复请求；签到输出使用回调，避免不同用户的线程争用全局标准输出。
- 移除自动跨游戏签到候选，保留显式配置；修复签到结果中全角和半角 `gameId` 文案的清理。
- 新增覆盖登录状态、接口协议、日志及并发写入的离线回归测试。

`ds` 算法参考：[NTEUID 塔吉多客户端](https://github.com/tyql688/NTEUID/blob/main/NTEUID/utils/sdk/tajiduo.py)、[taygedo-auto-attendance 协议实现](https://github.com/zzstar101/taygedo-auto-attendance/blob/main/src/taygedo/protocol.ts)。
