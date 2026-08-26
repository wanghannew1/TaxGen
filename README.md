# 个税模板填表工具 (TaxGen)

基于 Oracle 11g 数据库的税务申报 Excel 模板自动生成工具，从工资数据自动生成 4 种个税申报模板。

## 功能

- **正常工资薪金所得** — 29 列模板，含验证公式（左=右校验）
- **劳务报酬所得** — 14 列模板
- **全年一次性奖金收入** — 11 列模板（仅含奖金 > 0 的记录）
- **人员信息采集导入模板** — 51 列模板（自动解析身份证性别/出生日期）
- **Web 界面** — 月份选择、模板勾选、一键生成、在线下载、验证报告

## 环境要求

- Python 3.12
- uv（包管理工具，安装方式见下方部署步骤）
- Oracle Instant Client 23.4（已安装在 `/opt/oracle/instantclient_23_4`）
- Oracle 11g 数据库访问权限（10.0.0.8:1521:orcl）

## 部署

### 1. 进入项目目录

```bash
cd /home/ubuntu/github/TaxGen
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
DB_HOST=10.0.0.8
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

### 操作步骤

1. **选择月份** — 下拉框选择工资所属年月（如 2026年07月）
2. **选择模板** — 勾选需要生成的模板类型，或点击"全选"
3. **生成** — 点击"生成 Excel 文件"按钮，等待生成完成
4. **下载** — 在结果列表中点击"下载"按钮保存 Excel 文件
5. **验证** — 查看验证报告中的通过/失败统计

### 验证公式

验证逻辑与 demo C# 算法一致：

```
左 = 本期收入 - 养老个人 - 失业个人 - 医疗个人 - 公积金个人 - 年金(0)
右 = 实发工资 + 个人所得税 - 免税
通过 = |左 - 右| < 0.01
```

其中：
- `本期收入 = 应发工资 - 补缴及退款保险(个人) - 大病险(个人) - 采暖费 - 独生子女费`
- `免税 = 大病险(个人) + 采暖费 + 独生子女费`

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
