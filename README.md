# 个税模板填表工具 (TaxGen)

基于 Oracle 11g 数据库的税务申报 Excel 模板自动生成工具，从工资数据自动生成 4 种个税申报模板。

## ⚠️ 数据库安全规则（最高优先级，必须遵守）

> **Oracle 数据库只读，禁止任何写入操作！**

Oracle（工资业务库，`.env` 的 `DB_HOST`）包含 TC93/TC8M/TC90/AC01/TB93 等核心业务表，
**由外部业务系统管理，TaxGen 仅作只读查询**。

- ❌ 禁止在 Oracle 中创建/修改/删除任何表、数据、结构（CREATE/INSERT/UPDATE/DELETE/ALTER/DROP 等一律禁止）
- ✅ 只允许只读 SELECT 查询（参数化绑定变量）
- ✅ 应用自身需要持久化的配置数据（如特殊结算单元排除规则）**一律存储到自建 SQLite**：
  - `config_db.py` → `config.db`（特殊结算单元配置）
  - `tax_return.py` → `tax_return.db`（回盘比对数据）
- 代码约束：`db.py` 只建立连接池（无 DDL/DML）、`queries.py` 纯只读查询层
- 详细规则见 [AGENTS.md](AGENTS.md)

违反此规则 = 生产事故级错误。

## 功能

- **正常工资薪金所得** — 30 列模板，含验证公式（左=右校验，经济补偿金从实发中扣回）
- **劳务报酬所得** — 14 列模板
- **全年一次性奖金收入** — 11 列模板（仅含奖金 > 0 的记录）
- **人员信息采集导入模板** — 51 列模板（自动解析身份证性别/出生日期）
- **跨月合并规则确认** — 跨月发放人员三险一金翻倍/单倍逐人确认（依据历史申报档案判定，含置信度提示、ATC93BE 借支强制单倍）
- **零申报确认** — 本期收入为0人员生成/跳过逐人确认，支持特殊结算单元配置
- **历史申报库** — 导入个税端导出文件（税款计算等 4 类），支撑合并规则判定与回盘比对
- **增减员比对** — 个税端人员与发薪/未发薪/合同签署比对，生成增员/离职/待确认名单 + 验证明细
- **回盘比对 / 年度调整** — 税务端回盘文件比对、个人所得税年度调整
- **Web 界面** — 月份选择、批次搜索、待报列表、检查确认弹窗、一键生成、在线下载、验证报告

> 业务规则与日常操作详见 `docs/`（操作指南、合并规则分析、零申报规则、减员判定等）。

## 环境要求

- Python 3.12
- uv（包管理工具，安装方式见下方部署步骤）
- Oracle Instant Client 23.4（已安装在 `/opt/oracle/instantclient_23_4`）
- Oracle 11g 数据库访问权限（172.16.0.60:1521:orcl）

## 部署

### 1. 进入项目目录

```bash
cd /home/vod/code/tax-gen   # 替换为你实际克隆仓库的位置
```

### 2. 安装 uv 并创建虚拟环境

安装 uv（若未安装）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

创建虚拟环境并激活：

```bash
uv venv
source .venv/bin/activate
```

### 3. 安装依赖（使用清华镜像）

```bash
uv pip install -r requirements.txt --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

> 若需切换回官方源，去掉 `--index-url` 参数即可；也可以设置环境变量 `UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple` 全局生效。

### 4. 配置数据库连接

复制并编辑环境配置文件：

```bash
cp .env.example .env
```

编辑 `.env`，填入实际的数据库密码：

```
DB_HOST=172.16.0.60
DB_PORT=1521
DB_SERVICE_NAME=orcl
DB_USER=ccrcpq
DB_PASSWORD=你的密码
```

### 5. 验证数据库连接

```bash
LD_LIBRARY_PATH=/opt/oracle/instantclient_23_4 .venv/bin/python -c "
from db import init_db, get_connection
init_db()
conn = get_connection()
print('数据库连接成功')
"
```

### 6. 注册 systemd 服务（开机自启）

项目已附带 systemd 服务单元文件 `deploy/taxgen.service`，一键安装脚本会自动拷贝单元文件、设置开机自启并启动服务：

```bash
sudo bash deploy/install_systemd.sh
```

安装完成后服务以 `ubuntu` 用户运行，监听 `0.0.0.0:5000`，进程崩溃自动重启。

常用命令：

```bash
sudo systemctl status taxgen      # 查看状态
sudo systemctl restart taxgen     # 重启
sudo systemctl stop taxgen        # 停止
sudo systemctl disable taxgen     # 取消开机自启
sudo journalctl -u taxgen -f      # 查看实时日志
```

> 手动修改服务配置后需执行 `sudo systemctl daemon-reload && sudo systemctl restart taxgen`。

## 使用

### 启动 Web 服务

生产环境（推荐）使用 systemd 管理，开机自启、崩溃自动重启：

```bash
sudo systemctl start taxgen
```

手动前台启动（调试用，`FLASK_DEBUG=1` 开启调试模式）：

```bash
LD_LIBRARY_PATH=/opt/oracle/instantclient_23_4 FLASK_DEBUG=1 .venv/bin/python app.py
```

浏览器打开 `http://localhost:5000`（远程访问用服务器 IP）

### 操作步骤（简易版）

> 详细图文说明见 [docs/操作指南.md](docs/操作指南.md)（含每个弹窗/按钮含义、合并规则判定表、常见问题）。

一个月度报税的标准流程：

1. **选发放月份** — 顶部下拉框选择本次报送的发放月份（如 2026年07月）
2. **搜索批次** — 可按结算单元/所属月/批次号/经办人搜索，勾选本次报送的批次 → 「添加到待报列表」
3. **检查合并规则** — 点「检查合并规则并确认」：处理跨月发放人员的三险一金**翻倍/单倍**，逐人确认后关闭
4. **检查零申报** — 点「检查零申报并确认」：确认本期收入为0人员是否**生成零申报**
5. **生成** — 勾选模板 → 「生成报税数据」，等待生成完成
6. **下载 & 验证** — 结果列表下载 4 类 Excel，查看验证通过/失败统计

推荐首次使用时先在「历史申报库」导入上个申报期的个税端导出文件（税款计算 xls），合并规则判定才更准确；不导入也可用（判定保守为单倍）。

其他页面：回盘比对、年度调整、增减员比对、特殊结算单元配置（详见 [docs/操作指南.md](docs/操作指南.md)）。

### 验证公式

验证逻辑与 demo C# 算法一致（2026-09-08 重构，代数上与旧公式逐行等价，全量 20,025 条逐行分类 20,025/20,025 一致）：

```
左 = 本期收入 - 养老个人 - 失业个人 - 医疗个人 - 公积金个人 - 意外险个人 + 本次免税(ATC936)
右 = (实发工资 - 经济补偿金) + 税后扣除工会会费 + 个人承担代理费 + 个人所得税 + 个人其他调整(ATC93AG)
通过 = |左 - 右| < 0.01
```

其中：
- `本期收入 = 工资总额(ATC93AA) - 本次免税(ATC936) - 大病险个人(ATC93BD) - 补缴及退款保险差额个人(ATC93BE) + 个人交纳现金(ATC93X3) - 个人欠款(ATC93E)`
- 本次免税(ATC936) = 采暖费(ATC93W21) + 独生子女费(ATC93W4)；大病险个人(ATC93BD) 左右两侧同项（收入已减、实发已刨除），销项不再单列
- 个人其他调整(ATC93AG) 实发已扣、右式加回复原；经济补偿金(ATC93M) 属一次性补偿收入，含在实发金额中但不参与正常工资薪金验算，故从实发中扣回；验证报告另按 3×年平均工资（默认 12 万，UI 可配置）判断是否达交税标准

### 命令行生成

不启动 Web 服务，直接生成 Excel 文件：

```bash
LD_LIBRARY_PATH=/opt/oracle/instantclient_23_4 .venv/bin/python -c "
from db import init_db, get_connection
from queries import get_salary_records
from templates_gen.normal_salary import generate_normal_salary

init_db()
conn = get_connection()
records = get_salary_records(conn, 202607)
result = generate_normal_salary(records, '劳务派遣人员工资发放表202607', 'output')
print(f'生成完成: {result.file_path}')
print(f'记录数: {result.record_count}')
print(f'验证通过: {result.validation_pass}, 失败: {result.validation_fail}')
"
```

### 运行测试

```bash
LD_LIBRARY_PATH=/opt/oracle/instantclient_23_4 .venv/bin/python -m pytest tests/ -v
```

## 项目结构

```
TaxGen/
├── app.py                  # Flask 主应用
├── config.py               # 配置读取（.env）
├── db.py                   # Oracle 连接池
├── models.py               # 数据模型
├── queries.py              # SQL 查询层
├── validate_db.py          # 数据库探索脚本
├── templates/
│   └── index.html          # Web UI 页面
├── templates_gen/
│   ├── __init__.py
│   ├── normal_salary.py    # 正常工资薪金所得 (29列)
│   ├── labor_service.py    # 劳务报酬所得 (14列)
│   ├── annual_bonus.py     # 全年一次性奖金收入 (11列)
│   ├── personnel_info.py   # 人员信息采集导入模板 (51列)
│   └── validation.py       # 数据验证模块
├── tests/
│   ├── conftest.py
│   └── test_integration.py # 18 项集成测试
├── output/                 # 生成的 Excel 文件
├── deploy/
│   ├── taxgen.service      # systemd 服务单元文件
│   └── install_systemd.sh  # systemd 一键安装脚本
├── requirements.txt
├── .env.example
└── .env                    # 实际数据库配置（不入库）
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Web 界面 |
| GET | `/api/months` | 获取可用月份列表 |
| POST | `/api/generate` | 生成 Excel 文件 |
| GET | `/api/download/<filename>` | 下载生成的文件 |
| GET | `/api/validate/<month>` | 获取验证报告 |

### POST /api/generate

请求体：
```json
{
  "month": 202607,
  "templates": ["normalSalary", "laborService", "annualBonus", "personnelInfo"]
}
```

响应：
```json
{
  "files": [
    {
      "name": "正常工资薪金所得_20260819120000.xlsx",
      "type": "正常工资薪金所得",
      "count": 19969,
      "validation_pass": 5669,
      "validation_fail": 14300,
      "download_url": "/api/download/正常工资薪金所得_20260819120000.xlsx"
    }
  ]
}
```

## 数据来源

- **TC93** — 工资发放主表（~110 万行）
- **TC94** — 工资扣款明细表（~220 万行）
- **AC01** — 人员信息表

所有 SQL 使用参数化查询（bind 变量），无字符串拼接，纯读取操作。

## 注意事项

- 首次启动会自动初始化 Oracle 连接池，约需 2-3 秒
- 202607 月份有 19,969 条工资记录，生成 Excel 约需 1-2 秒
- 验证失败的记录通常是因为应发工资为 0 但实发工资不为 0（非正常工资薪金类记录）
- 生成的文件保存在 `output/` 目录，可通过 Web 界面下载
