# 道路占掘通行裁定台

为市政交通协同员与公共出行服务提供离线合成公开来源的时效和证据核验。核心结果：判定指定时段路线是否可达并核验绕行告知。公开文本经来源身份和版本采信后，分别驱动业务案件与履约核验；这三个子系统有独立业务实体、状态和 CLI 入口。

## 运行

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m roadcase demo
python -m pytest -q
```

手动运行：`python -m roadcase adopt --db state.db --input fixtures/road_normal.json`，`python -m roadcase assess --db state.db --input fixtures/route_normal.json`，`python -m roadcase settle --db state.db --input fixtures/notice_normal.json`，`python -m roadcase report --db state.db --out report.json`。

## 状态与证据

- 工区采信：选择具有有效时段和机关优先级的占道约束，留存原文摘要、修订、冲突和取消；同来源同版本异文不能覆盖旧证据。
- 通行推演：依据采信工区和时间窗决定可达或绕行，案件绑定来源修订，公共信息变更会把旧案件标为失效。
- 告知履约：判断绕行信息是否按时覆盖受影响群体，回执逐项验版本、目标和时点；部分履约与逾期保留为独立结论。

正常和失败输入位于 `fixtures`。SQLite 使用事务保存新修订、业务评估与回执；`report` 从库中重建来源摘要、案件和履约状态。模拟来源不代表真实法律、医疗或安全结论；生产接入需要对外部来源真实性和凭据管理另行评估。

## 绕行容量协调

`detour-config` 登记绕行边容量，`detour-reserve` 为具体旅程预留流量；超额旅程持久进入 waiting，`detour-release` 释放后按序提升候选。此流程有独立的 `detour_pool` 与 `detour_hold`，不是路线报告的参数。通行推演读取同工区版本的真实预留：竞争失败使原本可绕行的旅程变为不可达，重新预留又会生成不同的告知义务；工区修订使旧预留失效。见 `fixtures/detour_normal.json` 和 `fixtures/detour_conflict.json`。

## 本地 OCR 词框采集

`ocr-register` 冻结一份本地 OCR 词框、候选文本与置信度，`ocr-preview` 只按页和坐标拼接最高置信词，不提取结构化公告。结构化提取走 `ocr-extract --id <scan> --segment <路段> --policy <strict_literal|corroborated>`：先按词框几何划分表头、行、列、跨行续接与页眉/页脚角色（一个词框的一个候选只供一个字段），再联合来源编号/修订/时点、表行车道数、跨页独立说明与机关/通行能力约束求解，要求日期有效、`0≤封闭≤总车道`、`总车道=封闭+剩余`、同路段跨页重复事实一致；求解按行局部裁剪，不枚举全扫描候选的笛卡尔积。两种放行合同：`strict_literal` 要求每个必需字段由单个直接标注且置信度≥0.9 的词框给出、同页无竞争候选，否则 review；`corroborated` 允许低置信候选，但只有归属、跨页说明与表格算术共同留下唯一公告才 ready_for_adoption——无解为 conflict，多解为 review 并返回两份不同解析见证。每次提取落库为不可变 extract_revision；`ocr-export` 仅对 ready_for_adoption 导出可供 `adopt` 读取的 JSON（附 provenance 摘要，不改写既有 source_event），`ocr-verify` 从冻结词框重算结构化候选与唯一性，词框、格子归属或导出 JSON 变动都会使旧 ready 结论失效；同一 scan 的新识别版本另建提取，不覆盖旧版本。样例见 `fixtures/ocr-bulletin.json`（双合同分歧）、`ocr-bulletin-ambiguous.json`（去掉独立说明后的多解）、`ocr-bulletin-conflict.json`（说明与表格矛盾）与 `ocr-bulletin-split.json`（三页错列、跨行续接与页脚）。

