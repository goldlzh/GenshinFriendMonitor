# T01 快速扫描身份复核失败调查与修复

日期：2026-11-11，Asia/Hong_Kong。

**已确认并修复一个可模拟复现的滚动校验缺陷：整轮采集完成后，回访历史行仍被要求到达整份名单之外的“新行”。该机制与 T01 日志阶段和两次 40 像素补滑一致。T01 原始图像、位移审计和 workflow JSON 在当前电脑不可访问，因此尚未完成真实素材离线重放，也未确认真实游戏已修复。**

## 工作区与只读证据

- 原 GenshinMusic 工作区在 main 上有既有修改，保持原样。
- 修复使用独立克隆 `C:/Users/goldl/.codex/repo-cache/GenshinFriendMonitor`，分支 `codex/fix-identity-revisit-alignment`，基于 v1.1.1 提交 `f849e5c01a3ed36417ba23c65c2363f1858f41f9`。原始测试目录均只读。
- 已读取 E:/GenshinFriendMonitor测试 下 T01、F01、F02 报告。F01/F02 真实主页识别的成功结论保留，本次没有更改主页 OCR、界面或数据库代码。
- 原路径 `C:/新建文件夹/GenshinFriendMonitor测试/evidence/T01-20261111-194457/logs/` 不存在；E 盘证据目录只有 F01/F02 留存副本，没有 T01 全量快照。
- F02 的 `logs-readback/scan.log` 为 9,147 字节，SHA256 `135041ddbd2fb2ee480859b252b1efa7c7cc1a8159922d1891d7bee007c9c657`；`ocr_dump.log` 为 20,126 字节，SHA256 `0e3b84b305c93007615050cf5b443c27df8a97c6c01ec99abd0f21742b21a64f`。两者与 T01 报告记录的原扫描日志哈希一致，属于可直接核查的部分真实证据。未把私人 OCR 文本复制到源码或测试中。

直接核查到的真实日志时间线：

| 时间 | 事件 |
| --- | --- |
| 19:41:12 | 第一次运行因游戏/工具权限不匹配被阻断，未据此认定 OCR 失败 |
| 19:44:47 | 第 36 屏已核对 179/179 行，有效 179 |
| 19:44:47 | 后续页面发现同名或身份冲突，返回核对尚未确认的卡片 UID |
| 19:44:51 | 第 37 屏仍记录 179/179 行 |
| 19:44:53、19:44:55 | 两次“位移已确认，但新好友尚未完整露出，向下补滑 40 像素” |
| 19:44:57 | 校正后仍无法连续采集新的完整好友行，整轮未完成、保留原名单 |

`complete=false`、`committed=false`、预计 179 人及约 161.45 秒的 workflow 结论来自 T01 报告，当前未直接读取其 workflow JSON。第 36/37 屏图片、alignment 图片及 movement-failed 图片均未查看，不以报告描述替代实际图片检查。

## 具体失败路径与根因

1. `QuickScanWorkflow.run()` 完成列表覆盖后仍调用 `verify_late_ambiguities()`，这一顺序是必要的：179 行覆盖不代表 179 个身份都已确认。
2. `verify_late_ambiguities()` 保留 `self.slots` 中全部已采集观察，用于重叠身份核对；它返回列表顶部，并重置 `offset`、`origin` 以重新定位历史行。
3. 如果待核验目标不在顶部第一页，需要经 `advance_page()` → `FastScan.advance()` → `_advance()` 向下移动。
4. 原 `_advance.alignment()` 使用 `next_unseen = max(self.slots)+1`。此时 slots 仍含 0–178，所以下一要求是索引 179；而任何合法历史画面最多只到索引 178。正常回访也必然被判为 `no_new_rows`。
5. 在位移可信时，这会触发两次有限补滑，最后报连续行校正失败；如果额外补滑破坏了图像重叠，也可能先报相关度不足。

因此这是**本次遍历的页面推进位置与全局已采集覆盖范围混用**。不是把某条 OCR 文本读错就能解释的现象，也不需要改写扫描算法或主页解析器。

## 最小源码修改

生产代码只修改 `app/fast_scan.py` 的 alignment 判定：

```diff
- next_unseen = max(self.slots, default=-1)+1
- reason = ('row_gap' if first > next_unseen else
-           'no_new_rows' if last < next_unseen else '')
+ next_row = round((self.offset+before_bands[-1].top-self.origin)/layout.ROW_PITCH)+1
+ reason = ('row_gap' if first > next_row else
+           'no_new_rows' if last < next_row else '')
```

下一行基于当前画面最后一个完整卡片的实测位置，与 `add_page()` 使用相同的 offset/origin/ROW_PITCH 坐标体系。普通顺序扫描时它对应正常的下一行；身份回访时它对应当前历史画面的下一行，不要求增加全局 slots 数量。

保持不变：位移与相关度阈值、反向/停滞保护、完整行与缺口判断、有限校正次数、重叠身份比较、待复核 UID 流程、重复 UID 拒绝、最终标题/总数核对和数据库原子提交。没有清空 slots，没有绕过身份复核，没有仅因 179/179 就宣布成功。

## 先失败再通过的测试证据

新文件：`tests/test_quick_revisit_scroll.py`。全部为明确标注的模拟输入，不包含真实账号、截图或好友资料。

| 回归场景 | 旧代码 / 修复后结果 |
| --- | --- |
| 已有 179 行，可信历史页面位移 | RED：相同连续行错误，两次校正 `[40.0, 40.0]`；GREEN：一次正常推进，不做无谓校正 |
| 179 行首次采集，后页暴露同名，目标不在顶部第一页 | RED：slots=179、pages=37 后中止（无谓校正后相关度不足）；GREEN：继续回访，两个 UID 核验后才调用一次原子提交 |
| 顶部第一页之后的目标、部分底页和不同名单长度 | GREEN，身份与完整覆盖保持正确 |
| 已有名单中的同名回访 | GREEN，原有本地 ID 保持，无重复档案或错误增减事件 |
| 回访滚动失效 | 保持失败、不提交，原数据库及事件逐项一致 |
| 回访核验到重复 UID | 保持失败、不提交，原数据库逐项一致 |
| 历史观察重排 | 重叠身份检查拒绝，不继续错误核验、不提交 |
| 回访达到页数上限 | 保持失败，不跳过未核验目标，不提交 |

实际测试命令与结果：

```text
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -v
基线：805 项，706 通过，99 跳过，0 失败，0 错误（69.332 秒）
修复后：813 项，714 通过，99 跳过，0 失败，0 错误（78.165 秒）
锁定候选运行依赖后复跑：813 项，714 通过，99 跳过，0 失败，0 错误（81.227 秒）

.venv/Scripts/python.exe -X utf8 -m unittest tests.test_quick_revisit_scroll tests.test_scan_stride tests.test_stalled_scroll tests.test_selective_uid tests.test_quick_scan_workflow -v
相关套件：60 项，60 通过，0 跳过，0 失败/错误；其后新增历史重排用例已纳入上述完整套件
```

99 项跳过为原有私人素材回放，未将跳过记为通过。最初没有 .venv 路径时，805 项基线有两项管理员启动器测试失败；创建本地虚拟环境后基线变绿，未修改启动器或测试来消除失败。

独立代码审阅未发现 Critical、Important 或 Minor 项，审阅者独立执行 8 项新增测试通过。审阅不覆盖缺失的 T01 图像回放、99 项跳过场景或候选 EXE 实机行为。

## 尚缺的真实复现材料与边界

要完成 T01 离线重放，最小补充集合为该轮的：

- `workflow-latest.json` 或同轮 `workflow.json`；
- `movement.jsonl`；
- `pages.jsonl`（其中零基 page=35、36 对应日志第 36、37 屏）、`page-035.png`、`page-036.png`，以及 `movement-failed-before.png`、`movement-failed-after.png`；
- `alignment-037-1.png`、`alignment-037-2.png`；如存在，`row-gap-unconfirmed.png`。

scan.log/ocr_dump.log 的同哈希副本已具备，无需再为了相同内容复制。原始材料继续只读，不清理、不覆盖。只提供脱敏副本；打码若改变昵称/头像等位移匹配区域，会改变重放输入，须标明，不能声称与原图等价。也可先只提供脱敏 JSON 的位移、相关度、行索引等数值，不上传私人原图。

本修复证明该逻辑缺陷已在模拟中修复，并保留已有异常保护；还不能排除 T01 同时存在真实拖动滞后、图像匹配误差、界面变化或回访恢复问题。正式确认需要原测试电脑重新扫描，并核对最终身份、名单及提交状态。

## 测试候选包状态

可复测候选位于独立输出目录：

```text
C:/Users/goldl/.codex/outputs/gfm-t01-revisit-20261111/candidate2/package/
GenshinFriendMonitor-windows-x64-v1.1.1-t01-revisit1.zip
```

| 项目 | 实际结果 |
| --- | --- |
| ZIP 大小 | 166,874,045 字节 |
| ZIP SHA256 | `5bc6873ed60836e7a9bcac07f9f6bf80b3e5720752312acbb5719b722d2c4805` |
| EXE SHA256 | `7d43258e760e5eda17b8c66bfb9db208e02b7e6c05f2f00ac88ab0153d68f8ed` |
| 打包 | PyInstaller 6.21.0、Python 3.13.11 x64；58 份依赖许可证；ZIP 公共文件清单与 AMD64 检查通过 |
| 运行依赖 | requirements-local-lock.txt 所列版本均匹配；包含 det/rec/cls 模型、Qt qwindows 插件及 fontTools 支持文件 |
| 程序启动/OCR 冒烟 | 另见输出目录 smoke/smoke-result.json 的实际结果，不作为真实游戏验收 |
| 复测说明 | package/RETEST-T01.md；必须观察历史身份回访实际触发、complete/committed 和最终名单 |

首个 candidate1 构建发现缺少 fonttools，许可打包失败，未作为可复测包交付。随后仅在独立克隆的 .venv 内安装 requirements-local-lock.txt 指定版本，未卸载或更新虚拟环境外的全局包，重新完整测试、构建 candidate2。构建成功本身不等于干净系统或真实游戏验收通过。

不覆盖原始 v1.1.1 ZIP，不推送、不合并、不发布；程序内部版本仍为 1.1.1，候选文件名和附带清单单独标识修复版本。
