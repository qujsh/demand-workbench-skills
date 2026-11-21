# 构想台 Skills

将 Codex 本轮问题、完整回复及明确图片保存为构想台私密笔记，保留 Markdown 原文，自动生成三级标签。服务不可用时快速跳过，不补传历史内容。

## 安装

运行环境：macOS、Python 3.9+。脚本仅用 Python 标准库，自带 MCP 客户端。

推荐使用 [Skills CLI](https://github.com/vercel-labs/skills)（需要 Node.js 22.20+）：

```sh
npx skills add qujsh/demand-workbench-skills --skill demand-workbench-archive -a codex -g
```

也可从 [Releases](https://github.com/qujsh/demand-workbench-skills/releases/latest) 下载ZIP，将其中 `demand-workbench-archive` 文件夹放进 `~/.codex/skills/`。两种方式选一种。

安装完成后重新打开 Codex，发送：

> 使用 $demand-workbench-archive 帮我配置构想台问答归档，连接 https://api.freeligo.com/mcp。密钥由我在本机终端输入，不放进聊天。

创建本人 API Key，选择 `notes:create`、`notes:upload` 权限；账号也需具备对应权限。终端执行 Skill 的 `scripts/archive.py set-key`，按隐藏输入提示保存到 macOS 钥匙串。然后执行 `enable --url https://api.freeligo.com/mcp` 和 `check`。命令前加 `python3 <实际Skill目录>/`，在目标项目目录运行。check 不写笔记。

## 使用

仅保存当轮：向 Codex 说明「使用 $demand-workbench-archive 保存本轮问答及附件」。

希望各项目默认保存，明确告诉 Codex：

> 我授权默认将每轮问题、完整最终回复及明确图片保存到我的构想台。请按 Skill 的 setup.md 把规则写入实际生效的全局指令，保留原有内容。

安装本身不自动开启对话上传。全局指令变更可能需新开聊天加载。暂停时告诉 Codex「暂停构想台归档」，或执行 `disable`；恢复需明确要求。

## 线上与本地

新装默认 `https://api.freeligo.com/mcp`。地址优先级为已有配置 > `DEMAND_WORKBENCH_MCP_URL` > 线上默认地址。已有的 `http://localhost:8070/mcp` 配置保持不变，不会因离线切换到线上。用 `enable --url <地址>` 显式切换，并使用该环境对应的密钥。网页的 VITE 环境变量与 Skill 配置相互独立。

## 保存范围

只提交当前轮问答及明确图片，不采集历史会话、工具日志、隐藏推理或仓库文件。图片先直传存储，再通过 MCP 图片编号关联笔记。默认空标题、私密笔记、Markdown 原文、三级标签。密钥不随 Skill 分发。

每轮先做最多2秒的无正文门禁；失败按地址缓存60秒，期间跳过上传。图片核验临时故障当轮最多尝试3次，每次最多3秒；仍失败保存正文及已核验图片，标记部分成功。其他上传步骤失败不补传；queued只表示入队。依赖助手执行，遇到中断可能漏记，仍有传递正文的 token 开销，并非常驻后台同步器。

详细配置与限制见 [setup.md](skills/demand-workbench-archive/references/setup.md)。

## 开发与发布

```sh
python3 -m unittest discover -s skills/demand-workbench-archive/scripts -p 'test_*.py'
python3 tools/package_skill.py
```

推送 `v*` 标签后，GitHub Actions 验证脚本并创建含ZIP和SHA256SUMS的 Release。安装包只包含公开Skill源文件；不包含任何用户配置、密钥、聊天记录或图片。

核验策略和安全错误诊断从v0.1.1提供；搭配新版后端可返回准确存储错误码，并将服务端核验总预算限制为2秒。后端尚未升级时仍可保存正文，但不能准确分类旧版Internal error。
