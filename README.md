# AstrBot Plugin - 异环签到

异环自动签到插件，支持手机号密码登录、手机号验证码登录，以及定时签到。

## 功能

- **nte** (私聊): 执行当前账号签到，并返回签到结果与奖励明细
- **ntepw** (私聊): 输入手机号后，下一条消息输入密码完成登录
- **nteph** (私聊): 输入手机号后获取验证码，下一条消息输入验证码完成登录
- **ntelogout** (私聊): 退出登录并移除绑定
- **ntehelp** (全部): 查看命令帮助

## 使用

### 密码登录

1. 私聊发送 `/ntepw 手机号`
2. 按提示直接发送密码完成登录
3. 发送 `/nte` 执行签到

### 验证码登录

1. 私聊发送 `/nteph 手机号`
2. 按提示直接发送验证码完成登录
3. 发送 `/nte` 执行签到

### 定时签到

在插件配置中设置：

- `auto_sign_enabled`：自动签到开关
- `auto_sign_hour`：自动签到时间（小时，0-23）
- `auto_sign_delay`：每个用户签到前随机延迟秒数
- `max_users`：最大绑定账号数（0 为不限制）

## 安装

### 方式一：从 Release 下载

1. 前往 Releases 页面
2. 下载最新的 `astrbot_plugin_nte-vX.X.X.zip`
3. 在 AstrBot 插件页面上传该 zip 安装

### 方式二：使用 Git

```bash
cd /path/to/astrbot/plugins
git clone https://github.com/Candy-QAQ/astrbot_plugin_nte.git
```

## 依赖

插件依赖已在 `requirements.txt` 中列出，AstrBot 会自动安装。

## 许可

MIT License
