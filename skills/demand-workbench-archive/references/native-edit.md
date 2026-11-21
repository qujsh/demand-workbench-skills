# 原生编辑适配

私有config.json中设置 `"native_edit_enabled": true`；安装更新的脚本并启用后，普通publish与日志publish都会使用原生轮次映射。无需在消息里加编号。首次在线归档建立映射，随后编辑同一条Codex消息并重新发送，网站recordNo保持不变。

本机需要兼容的Codex App Server（experimental thread/turns/list、notLoaded视图）以及当前线程元数据state_5.sqlite/task_started.turn_attribution。脚本优先使用桌面内置codex，其次PATH；可在配置中以codex_binary指定路径。接口及本机事件结构可能随客户端更新而变化，无法读取或定位时停止，不回退新建。

元数据范围仅当前CODEX_THREAD_ID的轮次ID、触发类型及排列，不加载历史正文。只读列出持久化轮次，不启动或恢复当前会话。本机SQLite保存轮次→逻辑记录映射与快照；网站来源键仍按账号、项目、会话、逻辑记录隔离；实际原生轮次ID作为修订幂等键。

composer新建逻辑记录。edit_user_message通过有效历史的首个差异定位原消息；二次编辑延续同一逻辑记录，编辑较早消息不覆盖最后一条。没有旧映射、快照不完整、并发保存、原笔记不存在或被网页修改时停止。离线消息不补传，旧笔记不根据文本匹配接管。Codex撤回后续轮次不会删除网站历史。

回归验证：隔离CODEX_HOME中创建三轮、撤回中间轮、创建编辑轮，验证有效历史从三轮变为首轮+新编辑轮；路由测试覆盖重复编辑、早期编辑、未知目标、离线、失败入队回滚与原记录不存在。真实原生界面验收需在启用后发送一条新消息，再编辑重发，核对网站只有同一recordNo发生变化。关闭native_edit_enabled则恢复传统逐轮新建行为。
