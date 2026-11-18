# 道路占掘通行裁定台

本地合成临时标志、接近车道与遮挡物测绘可用 `python -m roadcase sign-record --db DB --input fixtures/sign-survey.json` 登记。`roadcase/signs.py` 仅保存视线证据；`roadcase/signplan.py` 另行裁定各车道在绕行决策点之前是否拥有足够、连续可读的标志视线。

## 临时绕行标志布设裁定（sign-decide / sign-verify）

`sign-decide` 基于冻结的测绘证据建立持久的布设计划与逐车道证书，`sign-verify` 从冻结原文独立重算并识别伪造。视觉结论与行程通行（`assess`）、走廊容量（`detour`）和乘客消息回执（`settle`）完全分离，后者都不是标志合格证据。

- 冻结内容：survey 原文 SHA、采信工区修订与当前状态、所选 `sign_id` 与 sign class、适用车道、几何单位与计算合同（引擎版本、容差、阈值与遮挡规则）。
- 视线模型：沿车道折线行进方向，以眼点到标志显示点的三维关系判定。标志朝向半视角之外不可读；障碍顶面在驾驶员眼平面上投出凸透视阴影（高于标志时为无界楔形，低于标志但高于眼高时为有界四边形），眼点落入阴影即被遮挡。顶高不高于眼平面的低护栏不产生阴影，不会仅凭平面投影误拦；切向零宽接触不计遮挡。
- 连续区间与提前量：FOV 锥与阴影多边形对每个直线段做精确半平面裁剪（无粗采样，极窄障碍不会漏算），取决策点之前最后一个连续可读区间，从其起点沿折线到决策点的弧长为实际 warning distance，与所选类别的 `min_warning_distance` 比较，等于阈值即满足。间断后的零散可见点不会被拼成持续提前量。
- 待核优先：无效坐标、非有限数值、缺失或偏离折线的决策点、未知类别、零眼高、退化朝向、工区版本冲突或引用了非当前有效修订，一律产出 `pending`，绝不默认为看得见。
- 版本与幂等：计划身份绑定测绘摘要、类别、车道、工区修订与当前状态；同证据重放幂等。测绘、障碍物、类别或工区新修订只会生成新计划，旧证书保留不覆盖。

手动运行：

```
python -m roadcase sign-record --db DB --input fixtures/sign-survey.json
python -m roadcase sign-decide  --db DB --survey sign-S1 --sign detour-D1 --class static-board
python -m roadcase sign-decide  --db DB --survey sign-S1 --sign detour-D1 --class led-message-board
python -m roadcase sign-verify  --db DB --plan PLAN_ID
```

种子测绘 `fixtures/sign-survey.json` 含两个仅用于本合成项目的类别合同（不代表真实交通规范）：中心车道 y=0、南车道 y=−6，决策点 x=100，标志显示点 (70,2,2.5)。高障碍盒 x=65..68、y=1.5..3、高 3m，低护栏盒 x=30..35、y=0.5..1.5、高 0.5m。精确结果：中心车道最后可读区间约 x=62..68、提前量约 38m；南车道约 x=38..62、提前量约 62m；低护栏从不遮挡。`static-board`（阈值 60m）仅南车道通过、不给全覆盖证书；`led-message-board`（阈值 30m）两车道均通过、给全覆盖证书。对照测绘 `sign-survey-no-truck.json`（移除高障碍）、`sign-survey-raised.json`（抬高显示点）、`sign-survey-upstream.json`（新增上游标志）作为新测绘/新计划，证明覆盖随可核实体证据改变而旧结论留存；`sign-survey-tricky.json` 覆盖折线转角、重复点与 0.2m 极窄障碍。

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

