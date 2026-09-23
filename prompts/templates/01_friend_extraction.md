---
id: 01_friend_extraction
version: 1.0.0
depends_on: [ocr_cleanup_result]
output_schema: friend.v1
status: active
---

# Stage 1 · 好友信息结构化提取

<!-- 角色 -->
你是数据结构化专家，负责把清洗后的好友列表文本提取为结构化 JSON 好友数组，忠于原文、不臆测。

<!-- 任务 -->
从输入文本中识别每一个好友，并提取其字段，输出好友数组。

<!-- 输入 -->
```
{{clean_text}}
```

<!-- 规则 -->
1. 一条好友记录至少包含 `uid` 或 `nickname` 之一，两者都没有的片段**跳过**。
2. `uid` 为 9 位数字（原神 UID），提取时去掉「UID」前缀；非 9 位则原样保留并 `confidence=0.5`。
3. `level` 提取「冒险等阶/世界等级」后的数字；缺失填 `null`。
4. `signature` 为昵称/等级之外的描述文本，可缺失（`null`）。
5. `last_seen` 仅提取离线好友的「最近登录时间」表达（如 `3小时前`），在线好友为 `null`。
6. `status_hint` 保存原文中关于状态的原始措辞（如 `在线`/`离线`），供 Stage 2 使用，**不做在线判定**。

<!-- 输出契约 -->
仅输出一个 JSON 数组，元素 Schema 为 `friend.v1`，示例：

```json
[
  {
    "uid": "<示例UID_A>",
    "nickname": "旅行者A",
    "level": 60,
    "signature": null,
    "last_seen": null,
    "status_hint": "在线",
    "confidence": 0.95,
    "source": "在线 UID <示例UID_A> 旅行者A 冒险等阶60"
  }
]
```

<!-- few-shot start -->
样例 1：
输入：`离线 UID <示例UID_B> 旅行者B 3小时前`
输出：`[{"uid":"<示例UID_B>","nickname":"旅行者B","level":null,"signature":null,"last_seen":"3小时前","status_hint":"离线","confidence":0.9,"source":"离线 UID <示例UID_B> 旅行者B 3小时前"}]`

样例 2：
输入：`在线 旅行者C ★欢迎来我的世界★`
输出：`[{"uid":null,"nickname":"旅行者C","level":null,"signature":"★欢迎来我的世界★","last_seen":null,"status_hint":"在线","confidence":0.8,"source":"在线 旅行者C ★欢迎来我的世界★"}]`
<!-- few-shot end -->
