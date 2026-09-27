# Interlude：持续生活叙事插件

Interlude 是一个面向 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的角色叙事插件。它让角色拥有独立、可持久保存的故事状态：记住已经发生的事，感知时间与日程，在合适的时候回复，也可以选择延迟回复或不回复。插件适合希望角色有连续生活感、长期关系变化和明确行为边界的使用场景。

> 当前版本：**1.1.0**。支持 AstrBot 4.24.x 至 4.x、Python 3.12 及以上。核心流程已经用模拟模型和消息发送器做过本地测试；正式使用前仍建议在自己的 AstrBot 平台上验证消息收发。

## 主要功能

- **持续故事**：消息、场景、角色状态和重要事件保存在 SQLite 中，AstrBot 重启后可以接着运行。每个私聊会话默认拥有独立故事，也可以配置为共享故事。
- **有选择的回复**：叙事引擎综合当前事件、关系、日程和现实条件，决定立即回复、延迟回复或暂不回复。连续消息可以在短时间内合并处理。
- **长期记忆与关系**：维护事实、待办意图、参与者关系、角色视角和演化层；角色的原始设定与后续经历分开保存。
- **时间与日程**：支持日程规划、生活自动推进、提醒、后续联系和有条件的主动消息。主动联系默认关闭，并受白名单、安静时段、冷却时间和次数限制。
- **群聊与图片**：群聊可单独启用。普通群消息可以进入故事背景；被提及或收到回复时，角色再判断是否发言。图片通过具备视觉能力的模型生成临时文字观察，不保存图片字节。
- **世界书联动**：可选接入 [astrbot_plugin_worldbook](https://github.com/Zhalslar/astrbot_plugin_worldbook)，在叙事生成时使用它管理的关键词规则和世界观条目。
- **管理入口**：提供管理员命令，以及 Dashboard、Story、Memory、Schedule 页面，用于查看故事、记忆、日程和运行状态。
- **故事初始化**：在“故事总览”中可格式化当前选中的故事，清除剧情与衍生数据并恢复运行状态；操作需输入 `CONFIRM`，角色设定、世界设定和参与者仍会保留。
- **现实时间线**：面板按配置的时区显示事件与上次叙事推进时间，并持续更新现实时间。故事游标是最后处理的时间，不会伪装成持续走动的时钟。

## 安装与开始使用

1. 将本仓库安装到 `AstrBot/data/plugins/astrbot_plugin_interlude`，安装 `requirements.txt` 中的依赖，然后重新加载插件。也可以通过 AstrBot 支持的插件安装方式安装本仓库。
2. 在 AstrBot 插件配置中填写角色名称、简介及其他设定，按需选择叙事模型。`narrative_provider` 留空时使用当前会话的聊天模型。
3. 打开配置中的 **“启用插件”**。发送私聊消息，使用 `/interlude status` 查看状态。
4. 需要世界书时，再安装并启用世界书插件，并打开紧挨着“启用插件”下方的 **“启用世界书联动”**。此项默认关闭，不影响单独使用 Interlude。

Plugin Pages 需要 AstrBot 4.24.1 或更新版本；核心私聊功能可在 4.24.0 运行。数据保存在 AstrBot 数据目录下的 `plugin_data/astrbot_plugin_interlude/interlude.db`。

## 世界书如何联动

开启 `worldbook_enabled` 后，私聊消息、群聊提及或回复进入叙事生成时，Interlude 会把本轮文本及发送者、会话信息交给已加载的世界书插件。世界书依照自身的关键词、作用域、优先级、生效时长、使用次数和通配符规则，把命中的条目加入本次叙事生成的系统提示。连续消息合并后按合并文本判定一次。

条目仍在世界书插件中创建和管理；Interlude 原有角色设定与故事状态继续生效。视觉观察、记忆压缩、日程规划等辅助模型调用和自动生活推进事件不触发世界书规则。世界书没有安装或未启用时，Interlude 使用原来的提示词继续运行。

## 工作方式与配置

消息进入后，插件依次进行消息合并、事件路由、上下文构建、叙事判断、数据库提交和消息发送。用户消息会立即记录；只有平台确认发送成功的角色消息才记为已经说过。新消息到来时，尚未发送的旧回复会失效，避免过时内容继续发出。

配置按 General、Character、Narrative、Memory、Schedule、Alter、Delivery、Proactive、Debug 分组。常用选项如下：

| 配置项 | 作用 |
| --- | --- |
| `enabled` | 启用 Interlude，默认关闭 |
| `worldbook_enabled` | 启用世界书联动，默认关闭，配置面板中位于 `enabled` 下方 |
| `character_name`、`character_profile` | 角色名称与简介 |
| `story_id`、`shared_story` | 故事标识，以及是否让用户共享同一故事 |
| `narrative_provider` | 指定叙事模型；留空跟随会话模型 |
| `message_merge_window_seconds` | 连续消息的合并时间窗口 |
| `group_enabled` | 开启群聊观察与发言，默认关闭 |
| `proactive_enabled`、`proactive_whitelist` | 开启主动联系并限定可联系的用户，默认关闭 |

调度器在重启后会从数据库恢复待处理的意图和任务。自动推进只在达到设定间隔且故事需要推进时执行，不会每分钟调用模型。模型输出失败时，输入事件仍会保留，不会错误地推进故事游标。

## 管理命令与页面

管理员可使用 `/interlude status`、`timeline`、`context`、`script`、`intents`、`advance`、`pause`、`resume`、`doctor`、`memory`、`schedule` 和 `overlay` 查看或管理状态。例如 `/interlude memory add <内容>` 添加记忆，`/interlude schedule refresh` 刷新日程。删除记忆、清除演化层、重置或清空故事的命令需要输入确认词；也提供下划线形式的命令别名。

Dashboard、Story、Memory、Schedule 页面提供故事选择、筛选与详情查看。故事时间线是 Interlude 自己保存的记录，与 AstrBot 的普通会话历史不同。

## 数据与排查

数据库包含原始用户消息与生成的事实，请妥善保管并备份。图片字节不会写入数据库；图片识别得到的文字事实可能进入故事记录。普通日志不会记录完整提示词、消息正文或密钥。平台发送失败不会被记为角色已发言；发送过程中发生崩溃时，该次投递会被标为状态不确定，不会自动重复发送。

- **没有回复**：角色可能选择暂不回复。检查 `/interlude status`、模型配置和 `/interlude doctor`。
- **回复较慢**：检查消息合并窗口、模拟输入间隔和待处理意图。
- **没有主动联系**：确认主动联系开关、白名单、安静时段、冷却时间与每日次数限制。
- **找不到故事**：在 Story 页面或 `/interlude timeline` 查看。

## 开发与说明

在 Python 3.12 环境中安装插件依赖、`pytest` 与 `ruff` 后，可从工作区父目录运行 `python -m pytest astrbot_plugin_interlude/tests -q` 和 `python -m ruff check astrbot_plugin_interlude`。测试覆盖持久化、发送确认、取消、迁移、日程、记忆、关系及重启场景；发布前仍需在真实 AstrBot 环境中联调。

本项目采用 [MIT 许可证](LICENSE)。设计思路参考了 [HDS Interlude / MomoiCore](https://gitee.com/MomoiCore/hds-interlude)，代码为独立的 Python 实现，没有复制或翻译其源代码。当前版本尚未实现向量检索、MCP/Agent 工具集成、多角色操作和导入导出。
