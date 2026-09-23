# 原神好友状态监测器：提示词设计草案

此目录保留 OCR 文本清洗、结构化提取、状态汇总和快照对比的提示词与 JSON Schema 草案。**它们没有接入扫描器运行时，不代表已实现的大模型功能。** 当前程序使用本地 OCR 和代码规则，不会因为存在这些文件而调用云端模型。

草案中的规则可能与当前扫描器不同，例如旧状态模板将不确定结果归为离线，旧身份模板允许昵称/等级回退；当前扫描器使用明确的未知状态和更严格的身份对应。开发时应以 [当前扫描流程](../SCAN_WORKFLOW.md) 为准，不能直接把模板输出用于覆盖正式好友档案。

## 文件

| 阶段 | 模板 | 用途 |
| --- | --- | --- |
| 0 | [00_ocr_cleanup.md](templates/00_ocr_cleanup.md) | OCR 文本清洗草案 |
| 1 | [01_friend_extraction.md](templates/01_friend_extraction.md) | 好友字段提取草案 |
| 2 | [02_online_status.md](templates/02_online_status.md) | 状态快照草案 |
| 3 | [03_statistics.md](templates/03_statistics.md) | 聚合统计草案 |
| 4 | [04_refresh_record.md](templates/04_refresh_record.md) | 每日快照对比预留 |
| 5 | [05_deletion_compare.md](templates/05_deletion_compare.md) | 名单差异预留 |

输出结构见 [schemas/output_schemas.json](schemas/output_schemas.json)。模板采用 frontmatter 标记版本、输入依赖和输出结构；其中 `active` 仅是原草案内部的阶段状态，不表示扫描器已启用该阶段。

## 示例与隐私

所有示例为虚构演示，UID 使用 `<示例UID_A>` 等非真实占位符，昵称使用通用示例名；占位符不是可供游戏身份核验的 UID。没有真实好友列表、截图、活动时段或账号凭据。

如要开发模型扩展，应先更新结构契约和未知状态语义、使用合成测试数据、明确用户授权及数据去向，再接入隔离评估。不要将个人 OCR 文本、UID 或扫描截图直接发送给外部服务。当前本地数据规则见 [PRIVACY.md](../PRIVACY.md)。
