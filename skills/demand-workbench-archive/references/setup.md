# 安装与配置

运行环境：macOS、Python 3.9+、可访问的构想台 MCP；运行脚本仅用标准库，无需npm、pip或后端仓库。其他操作系统尚未验证，默认钥匙串实现仅适用于macOS；已有环境变量DEMAND_WORKBENCH_API_KEY也可用。

本Skill所有命令都用 `python3 <skill目录>/scripts/archive.py <命令>`，工作目录保持目标项目，不要cd到Skill目录。Git项目使用仓库根目录归档；非Git项目使用当前工作目录。不同项目队列隔离，不读取项目文件内容。

## 首次启用

1. 默认连接线上 `https://api.freeligo.com/mcp`；用户准备具备notes:create、notes:upload权限的本人密钥，账户也需具备创建和上传权限。
2. 在本机终端执行 `set-key`（交互输入隐藏），或通过标准输入传入密钥，写入macOS钥匙串（服务名demand-workbench-codex-archive）；不要写入SKILL.md、配置、命令参数或分发包。已有同服务凭据无需重复设置。
3. 用户授权后执行 `enable --url <MCP地址>`；允许HTTPS或localhost/127.0.0.1/::1的HTTP，拒绝带凭据、查询和片段的地址。执行 `check` 验证工具；check不创建笔记。
4. 需要所有项目默认使用时，在用户全局Codex指令文件加入下面的规则。只有用户明确请求默认归档时才添加，不因安装Skill自动授权。保留文件已有内容；若存在非空AGENTS.override.md，应更新实际生效的文件，不能删除或绕过其内容。

全局规则模板（替换为本机实际Skill路径）：

```markdown
## 默认构想台问答归档（用户已授权）
在各项目主线程最终回复定稿后、发送前，应用 demand-workbench-archive Skill（<skill目录>/SKILL.md），先调用其scripts/archive.py gate（不传问答）检查；ready=false跳过本轮，ready=true才带availability_token把本轮问题、完整最终回复与明确附件交给publish。使用当前项目工作目录，不扫描历史或仓库文件。Markdown原样、标题为空、私密保存；用户要求停止或配置关闭时不提交。仅调用这个入口，不与项目旧脚本双重归档。门禁整次检查最多2秒，离线缓存60秒；仅明确本地端口关闭时允许快速TCP探测并在启动后提前完整检查，其他缓存内不发MCP请求；不入离线队列、不补传旧内容，不阻塞正常回复。
```

新全局指令需新建聊天/新会话加载；已有聊天未必立即刷新。Skill默认允许隐式调用；若看不到，重新打开Codex。官方依据：[全局AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)、[Skill发现](https://learn.chatgpt.com/docs/build-skills)。

## 本地状态和行为

- 配置：`${CODEX_HOME:-~/.codex}/demand-workbench-archive/config.json`，0600，enabled默认false，不保存密钥。`disable`全局暂停，`enable`恢复。
- 队列：同Codex目录下archives/demand-workbench-<项目路径摘要>/；目录0700、SQLite及图片0600，不进入项目仓库。与之前项目脚本的默认队列路径兼容，保留既有本地副本，但不再补传。
- 项目仅需通过`.codex/archive.local.json`的enabled=false退出，不需要复制Skill或密钥到每个项目。
- 本Skill自带MCP客户端；不使用项目配置中的upload_helper路径，不依赖原后端脚本。
- 图片最多9张一条、PNG/JPEG/WebP每张10MiB，入队时复制；长文拆分。服务不可用时跳过本轮；图片核验失败允许当轮最多3次即时尝试，每次总等待最多3秒，复用同一mediaNo，不重传文件；仍失败保存正文与已核验图片，任务为partial。其他上传步骤失败标记skipped，不补传。旧队列及中断任务不恢复；已成功分段/图片不回滚，本地副本不自动清理。不是常驻同步服务。
- 继承既有脱敏规则，不能保证识别自然语言或图片中所有秘密。标题为空、Markdown不转换、末尾标签服务端识别；重复标签及本地图片标记的隐藏是网站展示能力，旧版网站可能仍展示原始文字。

## 分发

分发整个Skill目录（SKILL.md、agents、scripts、references），不附本机config、钥匙串、队列、图片、日志或项目路径。接收者独立配置自己的网站与密钥；安装不自动写其全局归档授权。该版本是助手主动提交模式，不是自动读取对话的后台同步器。

## 离线门禁

gate不接收问答。按MCP地址共享离线缓存，整次握手/工具检查（包括钥匙串读取）通过独立进程限制在2秒内；失败缓存60秒，命中缓存不会延长截止时间。明确因本地端口关闭失败时，每轮新消息可用150ms内TCP探测提前发现启动，再在整次2秒预算内完整检查；仍失败沿用原截止时间并更新失败原因，防止端口已开却反复握手。到期无新对话不请求；服务恢复后只保存后续新问答。在线结果不跨轮缓存；gate返回一次性availability_token，publish消费它避免重复探测，120秒过期，绑定当前项目。

缓存和一次性凭据位于Codex目录的demand-workbench-archive/availability下，权限私有，不含问答或API密钥。显式check也遵守缓存。publish没有有效凭据时自行门禁，离线不建任务、不复制图片。后台进程只接收当轮job_id且尝试一次；默认drain、retry和旧Hook均不会扫描补传历史任务。仅MCP网络连接失败会写入离线缓存，图片存储核验失败不误判整个MCP离线；已完成写入不自动撤销。

## 三级归档标签

新版自动生成 `#Codex/项目名/聊天名`（未知聊天名时两级），需配合支持三级路径的构想台后端。每级最多30字，三级完整路径最多92字；正常项目名和聊天名完整保留，仅单级超长才采用「前缀~四位哈希」。发生缩写时，完整聊天名仍保留在归档任务元数据中；不修改历史笔记或重送旧任务。

## 服务地址与升级

地址优先级：已保存config.json中的url > 环境变量DEMAND_WORKBENCH_MCP_URL > https://api.freeligo.com/mcp。已有明确本地配置升级后不变；不因本地服务离线改投线上。

线上新装：`python3 <skill目录>/scripts/archive.py enable --url https://api.freeligo.com/mcp`。本地开发：`python3 <skill目录>/scripts/archive.py enable --url http://localhost:8070/mcp`。分别使用对应环境的本人密钥，钥匙串当前仅保存一个归档密钥；切换环境需同步更新凭据。网站VITE_API_BASE_URL/VITE_MCP_PUBLIC_URL只控制网页，不控制本机Skill。

安装：`npx skills add qujsh/demand-workbench-skills --skill demand-workbench-archive -a codex -g`。此安装方式需要Node.js 22.20+；没有Node时，从GitHub Releases下载ZIP，把demand-workbench-archive目录解压至~/.codex/skills/。两种方式选一种，避免同名Skill重复安装。安装后让Codex读取本Skill完成配置；默认归档需用户另行明确授权。

## 图片核验与排查（v0.1.1）

后端新版将云端stat核验总预算设为2秒（SDK连接/读写各1秒，隐藏重试关闭，有界并发2，无排队），Skill确认请求以子进程限制总等待3秒。超时、网络中断或明确的存储临时故障才重试，总计3次；不存在、大小/类型不符等明确业务失败只尝试一次。多图分别核验，等待预算按图片数累计。

最终核验失败不会关联未确认媒体；原始Markdown正文和其他已确认图片照常提交，partial表示正文已保存但存在缺图。create_note失败则skipped，不补传、不重新创建。status可查看任务error（安全JSON）及images中的confirmationAttempts/confirmationError；进度中的imageFailures按图片索引记录步骤、工具、错误码、请求编号、尝试次数和耗时。原始服务商响应及凭证不写入这些字段。

新错误码：S048105012=图片存储核验超时，S048105013=图片存储核验暂不可用。旧后端只返回Internal error时无法准确判别存储临时错误，不盲目重试，但仍会降级保存正文。后端改动需重启/部署后生效；仅升级Skill无法改变旧后端的SDK等待行为。

## 本地服务启动后提前恢复（v0.1.2）

仅localhost/127.0.0.1/::1的明确连接拒绝标记local_port_closed；连接超时、鉴权失败、地址非回环及旧unavailable缓存不推断为端口关闭。此类缓存每轮仅做一次TCP探测（最多150ms，localhost同时考虑IPv4/IPv6），不发送凭证和正文；端口恢复则立即完整检查，通过后移除缓存，仅保存当前轮。端口打开不等于MCP可用；完整检查失败保持原截止时间并更新失败原因。无新消息不轮询。

用户明确要求重新检查时：`python3 <skill目录>/scripts/archive.py check --force`；准备归档可用`gate --force`并消费返回凭据，不再重复gate。force仅忽略离线缓存，仍检查enabled、项目禁用及实际权限，不上传正文、不补传；失败不会延长已有未到期缓存。默认行为不对线上地址做缓存内额外探测。
