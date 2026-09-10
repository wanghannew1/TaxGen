# TaxGen 项目规则

## ⚠️ Bug 留痕规则（强制性）：凡是 bug，必须提交 issue

**任何被确认的 bug（无论是否已修复、是否由本次改动引入），都必须提交 issue 留痕。**

### 要求

1. **发现 bug 即提交 issue**，不得只修复不记录。修复后再提也允许（注明修复 commit）。
2. issue 必须包含：
   - 问题现象（含复现文件/月份/单位等关键参数）
   - 根因分析
   - 修复方法 + 相关 commit
   - 验证结果
3. **双平台提交**：主平台 **Gitee**（`wanghannew1/tax-gen`），同时必须同步到 **GitHub**（`wanghannew1/TaxGen`）。GitHub 提交流程：`gh issue create -R wanghannew1/TaxGen --title "..." --body-file <body> --label bug`（正文注明"与 Gitee <编号> 同步"）。
   历史上 bug 类 issue 均带 `bug` 标签，已修复的另加 `已修复` 标签。
4. 修复 commit 的说明中可引用 issue 编号，便于追溯。

### 示例

历史 issue 格式参考：`IKB54J`（连接池卡死）、`IKDRY9`（劳务报酬模板 5 项缺陷）、`IKAU2L`（多批次记录缺失+备注共用）；跨平台示例：GitHub `#5` ↔ Gitee `IKELVC`。

## ⚠️ 次高优先级安全规则：Oracle 数据库只读，禁止任何写入

**Oracle 数据库（工资业务库，见 `.env` 的 DB_HOST）是本系统的核心业务数据源，
包含 TC93/TC8M/TC90/AC01/TB93 等工资、合同、人员、结算单元表。
这些表由外部业务系统管理，TaxGen 仅作只读查询。**

### 禁止事项（绝对不允许）

1. **禁止在 Oracle 中创建任何表、索引、视图、序列等对象**（禁止 CREATE）
2. **禁止在 Oracle 中修改、插入、删除任何数据**（禁止 INSERT/UPDATE/DELETE/MERGE）
3. **禁止在 Oracle 中修改表结构**（禁止 ALTER/DROP/TRUNCATE）
4. **禁止调用任何 Oracle 存储过程/函数完成写入**
5. **禁止任何 DDL/DML 操作，包括事务性写入后回滚的操作**（连尝试都不允许）

### 允许事项

- 只读 SELECT 查询（含 JOIN、子查询、聚合）
- 使用绑定变量（bind variables）参数化查询

### 应用自身数据的存储规则

应用需要持久化的**配置/业务数据**（如特殊结算单元排除规则），
**一律存储到自建数据库**，不得写入 Oracle：

- **SQLite**（首选，零依赖）：`config_db.py` → `config.db`（项目根目录）
  - `special_unit_config` 表：特殊结算单元配置（工资为0不增员不报税 / 完全排除不增员不报税）
- `tax_return.py` 使用 SQLite `tax_return.db`（回盘比对数据）

**新增任何需要持久化的数据时，优先使用 config_db.py 的 SQLite 机制。**

### 代码约束

- `db.py`：只建立 Oracle 连接池，**不允许出现任何 DDL/DML 语句**
- `queries.py`：只读查询层，**不允许出现 INSERT/UPDATE/DELETE/CREATE 等语句**
- 需要跨库读取配置时：先从 SQLite (config_db) 读取配置值，
  再作为绑定参数传入 Oracle 查询（**禁止在 Oracle SQL 中引用 SQLite 表**，
  也禁止在 SQLite 中引用 Oracle 表）

### 违反后果

违反本规则将导致业务数据被篡改，属于**生产事故级错误**。
代码评审、AI 代理开发时必须检查此规则。