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

