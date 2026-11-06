# 各页面顶部导航菜单栏按钮数量不一致（7/10/11 不等）

## 问题现象
- 模块：前端页面顶部导航栏（templates/*.html 各模板共用但未统一）
- 现象：各页面菜单栏链接数量不一致，页面间切换时导航入口缺失或多出，用户体验不统一：
  - 大部分页面只有 7 个导航链接；
  - 部分页面有 10 个；
  - index.html 有 11 个（多了一个“使用说明”按钮，弹出 guideModal）；
- 预期：所有页面导航栏应保持相同的 10 个链接、相同顺序、相同高亮规则；仅 index.html 保留额外的“使用说明”按钮。

## 根因分析
各模板（index, tax-return, payroll-records, personnel-compare, special-units, filing-history, social-security, tax-compare, tax-forecast, tax-adjust）的顶部导航栏是各自独立的硬编码副本，历史迭代中未同步更新，导致菜单项数量与顺序不一致。

## 修复方法
- commit `6cbd570`（fix: 统一各页菜单栏为 10 个相同链接），已双推至 `origin`（Gitee 主仓）与 `github`（TaxGen）。
- 将 9 个模板的导航栏重写为规范的 10 链接，顺序与 index 对齐，并保留每页 `btn-primary active` 当前页高亮；人员增减员比对保持 `outline-success` 样式；仅 index.html 多保留“使用说明”按钮（弹出 guideModal）。
- 验证：curl 逐页抓取全部 10 个页面，确认每页均为 10 个链接且顺序一致。

## 规范链接顺序
/ 报税数据生成 → /tax-return 报税状态统计 → /payroll-records 工资查询 → /personnel-compare 增减员比对(outline-success) → /special-units 特殊结算单元 → /filing-history 历史申报库 → /social-security 历史社保 → /tax-compare 报税核对 → /tax-forecast 报税预判 → /tax-adjust 多退少补

## 验证结果
- 10 个页面均抓取到 10 个导航链接，顺序一致；
- 各页当前页高亮正确，增减员比对页保持 outline-success。

## 关联
- 修复 commit：`6cbd570`（已双推）
- 标签：`bug`、`已修复`；提交说明引用本 issue 编号。
- 与 Gitee 主平台 `wanghannew1/tax-gen` 同步提交，GitHub `wanghannew1/TaxGen` 需同步创建。
