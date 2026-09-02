# TaxGen 项目规则

## ⚠️ 最高优先级安全规则：Oracle 数据库只读，禁止任何写入

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