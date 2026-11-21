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

从v0.1.2起，本地服务明确因端口关闭而离线时，后续新消息会快速探测端口，启动后可在60秒缓存到期前恢复。鉴权/超时等失败继续使用缓存。用户明确要求立即重查时可执行`check --force`，不补传旧内容，不绕过关闭设置。

## 日志与修改（开发预览）

对Codex说「记一条日志：今天完成了第一次同步尝试」，网站仅保存日志正文和配图；之后说「把刚才那条日志改成：今天完成了第一次同步尝试，并验证了编辑效果」，更新同一条网站笔记。普通问答归档保持原样。启用原生编辑适配后，支持直接编辑并重新发送已建立映射的消息。

此模式沿用现有notes:create、notes:upload范围，并需notes:read、notes:update；不带图时不会上传图片。更新发现网页版本变化、公开、删除或编辑副本时拒绝覆盖；不会默默创建新笔记替代。记录映射只保存在当前机器，作用范围为项目、MCP地址和会话。初版限制单条正文14500 UTF-16单位及9张图。详见Skill中的日志模式。

### 直接输入文案记录日志

告诉 Codex“接下来我直接输入日志文案，帮我上传即可”，随后直接发送正文或配图。无需编号、标题或命令；Skill内部生成标识，只保存日志原文、用户图片和末尾标签，确认回复不写入正文。纯记录时回复保持简短；普通问答归档不受其他会话的日志偏好影响。启用原生编辑适配后，内部映射用于识别编辑重发；无法定位时停止，不猜测覆盖。

### 可选兼容：固定编号编辑同步（开发预览）

后端需包含 `get_codex_note`、`sync_codex_note` 及 `V202611210100` 迁移。现有线上旧版本不支持此入口，不会回退新建重复笔记。

```text
[日志:abc123]
今天尝试用 Codex 记录工作日志。
```

之后在 Codex 编辑这条消息，保留 `[日志:abc123]` 并重新发送，即更新同一条网站笔记。新编号保存新记录；编号不显示在网站正文。只保存日志及用户配图，助手简短确认不入正文。密钥需要 notes:read、notes:create、notes:update；图片还需 notes:upload。网页已手动修改时停止覆盖，离线跳过不补传。

详见 [使用说明](skills/demand-workbench-archive/references/numbered-journal.md)。

### 原生编辑同步（本机适配预览）

无需手写编号：启用native_edit_enabled后，直接编辑Codex已发送消息并重新发送，更新同一网站笔记。仅支持启用后建立映射的消息及兼容的本机客户端，旧笔记不自动接管。读取当前会话轮次元数据，不读取历史正文；版本冲突或身份不确定时停止。参见[适配说明](skills/demand-workbench-archive/references/native-edit.md)。
