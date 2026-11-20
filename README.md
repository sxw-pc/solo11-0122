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

## 开挖土方票据与去向证成

`spoil-open` 冻结采信工区版本与土方批次的声明量/去向；`spoil-ticket` 保存装车、暂存和入场的称重原始票据与父票据引用（允许 `gross_kg`/`tare_kg` 同为空表示未独立称重、`dry_fraction` 为空表示湿度证据缺失），`spoil-summary` 列出票据。

`spoil-seal` 把批次当前全部票据的 ID 与原文摘要、计量合同、工区版本、批准接收节点（可选含冻结在库余额 `balances`）封账为 `spoil_seal`；封账后新增票据只能另起封账，同 ID 异文票据或封账即冲突。计量合同二选一：`as_weighed` 要求湿质量在每个节点守恒；`dry_solids` 允许经独立干分数记录修正含水变化，但干固体质量必须在同一转运网络守恒，未知干分数不得当作 1。

`spoil-verify` 从冻结原票据独立重放并持久化 `spoil_result`：父票据须属同批且时间在子之前、地点相接，无循环、无票据/父边重复计入；以净重 ±uncertainty_kg 为非负湿质量区间、干分数乘同一净重得干固体区间，用精确有理数单纯形求同一组覆盖全部分装、合并、暂存与接收节点的全局可行质量赋值（同一父量不能被两条链各花一遍）。声明湿量约束根部开挖票据；终端物料必须进入批准接收节点或有冻结且明确的在库余额。结论为 `consistent_with_records` / `indeterminate` / `blocked`：缺父、未到场、无独立称重或湿度证据不全为 indeterminate，无票不视为零质量；结果保存每票据与每节点的入出区间、可行赋值或矛盾割/缺证据与原文摘要，篡改出票或汇总结果无法维持结论。工区新修订使旧封账失效，已存结果保留不变（`spoil-result` 可读）；票据一致仅说明记录在该口径下全局可行，不构成对现实世界无偷排的绝对保证。合成样例为 `fixtures/spoil-lot.json`、`spoil-load.json`、`spoil-accept.json` 与分装/合并网络 `fixtures/spoil-split-*.json`。

