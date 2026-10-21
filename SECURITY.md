# 安全策略

## 支持版本

本项目处于早期开发阶段，安全修复只在 `main` 分支的最新版本上进行。

| 版本 | 是否支持 |
| --- | --- |
| `main`（最新） | ✅ |
| 历史 tag | ❌（暂无正式 release） |

## 报告漏洞

**请不要开公开 Issue 报安全漏洞。**

请用 GitHub 的 [Security Advisory](https://github.com/mihayosima/prism/security/advisories/new) 私密报告，或邮件联系维护者。请在报告里给出：

- 影响面（哪个模块 / 端点 / 插件）
- 复现步骤（尽量给出最小用例）
- 影响后果（越权 / 数据泄露 / 远程执行 / 拒绝服务）
- 你已知的缓解方式（如果有）

我们会在确认后尽快回应并给出修复计划；修复合入前不会公开细节。

## 已知风险面

Prism 是一个 agent 运行时，**设计上就会调用模型与工具**，以下都是需要你自行收敛的风险：

1. **高危工具**：`plugins.windows_mcp` 会挂进任意命令执行、注册表读写、文件删除、进程终止、键鼠模拟等工具。工具调用虽过 guard 白名单，但**默认 `default=open`（放行）**。
   生产请在 `config["agent"]["permissions"]["task_deny"]` 里显式收紧 `mcp.windows.PowerShell` / `Registry` / `Process` 等，或直接停用该插件。
2. **默认无鉴权**：`python -m agent.run.chat` 不带 token 时，任何人能访问全部 `/api/*`（包括写插件、删文件）。**仅供本机开发**；部署到网络必须加 `--token --hardened`。
3. **凭据文件**：`local_secrets.json` 是明文，已被 `.gitignore` 排除，**请勿提交**。若不慎入库请立即轮换密钥。
4. **沙箱**：沙箱默认关闭（`mode=none`）。启用后强度由真实后端能力决定 —— WSL2 档为进程级 `partial`，Docker 档为容器级 `full`，两者都不可得时诚实报 `none`。它**不是**安全边界的替代品，不要用它来跑不可信代码。
5. **提示注入**：模型输出可触发工具调用。挂载了高危工具时，请把不可信输入当作潜在注入源，收紧白名单而不是寄望于模型自律。

## 安全配置基线（部署到网络前必做）

```bash
python -m agent.run.chat --token <随机令牌> --hardened
```

- `--hardened` ⇒ deny-by-default + 强制鉴权
- Redis 建议设 `PRISM_REDIS_REQUIRED=1`，避免续传能力静默降级
- 只暴露必要的工具，定期 review `task_deny` / `task_allow`

---

编制时间：2026-10-22 01:09（GMT+8）

| 版本 | 时间 | 变更摘要 |
| --- | --- | --- |
| v1.0 | 2026-10-22 01:09 | 初版：支持版本、私密报告渠道、五类已知风险面、部署安全基线 |
