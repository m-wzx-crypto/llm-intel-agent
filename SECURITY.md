# 安全说明

## 报告问题

如果发现安全问题，请不要直接开公开 Issue。请通过 GitHub 的私有漏洞报告（Security → Report a vulnerability）提交，或在 Issue 中只描述大致范围、不附带复现细节。

## 设计约束

- 服务只监听 `127.0.0.1`，不对局域网暴露
- 模型 API Key 存放于 macOS Keychain（service `LLMIntelAgent`），配置文件不保存明文
- 数据仅写入本机 `~/Library/Application Support/LLMIntelAgent/`
- 更新清单强制校验版本号、HTTPS、sha256 与文件大小后才允许替换应用

## 请不要提交

- 任何真实 Cookie、账号密码、API Key
- 采集到的原始数据库（`*.db`）与本地缓存
- 内网地址、部署主机、签名与公证凭据

仓库的 `.gitignore` 已覆盖上述内容，但提交前仍建议自行检查。
