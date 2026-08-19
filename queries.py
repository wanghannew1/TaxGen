"""queries.py - Oracle 11g 工资数据查询层

本模块提供从 Oracle 11g 数据库读取工资数据所需的全部 SQL 查询,
供 Flask 应用生成税务申报 Excel 模板使用。

数据表:
- TC93  工资主表 (~110 万行), 主键 ATC930, ATC931 = 工资所属年月 (YYYYMM)
- TC94  工资扣款明细表 (~220 万行), 通过 ATC930 关联 TC93
- AC01  人员信息表, 通过 AAC001 (人员ID) 关联 TC93

字段映射 (经 CSV export(4).csv 交叉验证 + Oracle all_col_comments):
- ATC93BE = 补缴及退款保险差额（个人）
- ATC93BA = 补缴及退款保险差额（单位）
- ATC93BD = 大病险（个人承担）
- ATC93BC = 大病险（单位承担）
- ATC93BH = 意外险（个人承担）
- ATC93BB = 意外险（单位承担）
- ATC93BF = 转款合计
- ATC93BG = 补缴及退款个人所得税
- BAA001 = 养老保险个人部分 (8%)
- BAA002 = 医疗保险个人部分 (2%)
- BAA003 = 失业保险个人部分 (0.3%)
- CAA002 = 住房公积金个人部分 (7%)

设计约束:
- 全部 SQL 使用参数化查询 (bind 变量 :name), 绝不拼接用户输入;
  IN 列表使用生成的 :bN 占位符 + 绑定值, 用户数据从不进入 SQL 文本
- Oracle 11g 兼容 (无 FETCH FIRST / OFFSET, IN 列表上限 1000 个表达式,
  故 get_deduction_details 分批查询)
- 数字字段统一使用 (val or 0) 模式处理 NULL, 默认 0.0
- 连接由调用方传入 (来自 db.get_connection()), 本模块不负责连接生命周期
- 不直接 import oracledb

注意: models.py 未定义 DeductionInfo (且约束禁止修改 models.py),
故 DeductionInfo 在本模块内定义; 其余 dataclass
(SalaryRecord / MonthOption / PersonnelInfo) 均来自 models.py。
"""

from dataclasses import dataclass
from typing import Dict, List

from models import MonthOption, PersonnelInfo, SalaryRecord

# TC94 AAA901 类别码 → DeductionInfo 字段名 (Task 3 探索发现)
_AAA901_FIELDS = {
    1: "应发项",          # 应发/工资项 (正数)
    2: "扣款",            # 扣款 (负数)
    3: "补发工资",        # 补发工资
    9: "伙食补助",        # 伙食补助
    10: "福利费",         # 福利费
    11: "保险个人部分",   # 保险个人部分
    12: "服装费",         # 福利费/服装费
}

# Oracle 11g IN 列表表达式上限为 1000 (ORA-01795), 分批大小留余量
_IN_BATCH_SIZE = 900


@dataclass
class DeductionInfo:
    """TC94 扣款明细 - 按 ATC930 (TC93 主键) 关联的工资扣款分解。

    注意: 养老/医疗/失业/公积金个人部分已在主查询中直接从 TC93
    (BAA001/BAA002/BAA003/CAA002) 读取, 本明细为补充信息。
    """
    atc930: int = 0
    应发项: float = 0      # AAA901=1
    扣款: float = 0        # AAA901=2 (负数为扣款)
    补发工资: float = 0    # AAA901=3
    伙食补助: float = 0    # AAA901=9
    福利费: float = 0      # AAA901=10
    保险个人部分: float = 0  # AAA901=11
    服装费: float = 0      # AAA901=12
    其他: float = 0        # 其他未识别 AAA901 码合计


def get_available_months(conn) -> List[MonthOption]:
    """查询 TC93 中所有可用的工资所属年月 (下拉框选项)。

    ATC931 格式为 YYYYMM (如 202607), label 显示为 "YYYY年MM月"。
    """
    sql = """
        SELECT DISTINCT ATC931, ATC932
        FROM TC93
        WHERE ATC931 IS NOT NULL
        ORDER BY ATC931 DESC
    """
    options = []
    with conn.cursor() as cursor:
        cursor.execute(sql)
        for row in cursor.fetchall():
            month = int(row[0] or 0)
            if month <= 0:
                continue
            label = f"{month // 100}年{month % 100:02d}月"
            options.append(MonthOption(value=month, label=label))
    return options


def get_salary_records(conn, month: int) -> List[SalaryRecord]:
    """按工资所属年月查询工资记录 (TC93 + AC01 左连接)。

    SQL 按规范选择全字段; 其中 基本工资/加班费/餐补/岗位工资/绩效奖金/
    tc930_id 在 models.SalaryRecord 中暂无对应字段, 仅作预留不映射。
    """
    sql = """
        SELECT
          t93.AAC001,
          t93.AAC003 AS 姓名,
          ac01.AAC002 AS 身份证,
          t93.ATC931 AS 工资所属年月,
          t93.ATC93X AS 基本工资,
          t93.ATC933 AS 应发工资,
          t93.ATC93C AS 实发金额,
          t93.ATC93D AS 个人所得税,
          t93.ATC93AA AS 工资总额,
          t93.ATC93W4 AS 独生子女费,
          t93.ATC93W21 AS 采暖费,
          t93.ATC93W1 AS 奖金,
          t93.ATC93W2 AS 加班费,
          t93.ATC93W3 AS 餐补,
          t93.ATC93W9 AS 岗位工资,
          t93.ATC93W10 AS 绩效奖金,
          -- 五险一金个人部分 (来自 TC93 本表, Task 3 验证)
          t93.BAA001 AS 养老个人,
          t93.BAA002 AS 医疗个人,
          t93.BAA003 AS 失业个人,
          t93.CAA002 AS 公积金个人,
          -- 大病险/补缴 (Oracle all_col_comments 确认, ATC930 用于 TC94 关联)
          t93.ATC93BE AS 补缴及退款保险金额个人,
          t93.ATC93BD AS 大病险个人,
          t93.ATC930 AS tc930_id
        FROM TC93 t93
        LEFT JOIN AC01 ac01 ON t93.AAC001 = ac01.AAC001
        WHERE t93.ATC931 = :month
        ORDER BY t93.AAC003
    """
    records = []
    with conn.cursor() as cursor:
        cursor.execute(sql, {"month": month})
        for row in cursor.fetchall():
            records.append(SalaryRecord(
                职工号=str(row[0] or ""),          # AAC001
                姓名=str(row[1] or ""),            # AAC003
                身份证=str(row[2] or ""),          # AC01.AAC002
                工资所属年月=int(row[3] or 0),      # ATC931
                应发工资=float(row[5] or 0),        # ATC933
                实发工资=float(row[6] or 0),        # ATC93C
                个人所得税=float(row[7] or 0),      # ATC93D
                工资总额=float(row[8] or 0),        # ATC93AA
                独生子女费=float(row[9] or 0),      # ATC93W4
                采暖费=float(row[10] or 0),         # ATC93W21
                奖金=float(row[11] or 0),           # ATC93W1
                养老个人=float(row[16] or 0),       # BAA001
                医疗个人=float(row[17] or 0),       # BAA002
                失业个人=float(row[18] or 0),       # BAA003
                公积金个人=float(row[19] or 0),     # CAA002
                补缴及退款保险金额个人=float(row[20] or 0),  # ATC93BE
                大病险个人=float(row[21] or 0),     # ATC93BD
            ))
    return records


def get_deduction_details(conn, atc930_list: List[int]) -> Dict[int, DeductionInfo]:
    """查询 TC94 扣款明细, 按 ATC930 聚合为 Dict[ATC930, DeductionInfo]。

    五险一金个人部分已在 get_salary_records 中直接从 TC93 读取,
    本明细为补充分解 (应发项/扣款/补发/补助/福利费等)。

    Oracle 11g IN 列表最多 1000 个表达式 (ORA-01795), 故将 ATC930 列表
    分批 (每批 900) 查询再合并结果; 空列表直接返回空字典。
    """
    if not atc930_list:
        return {}

    results: Dict[int, DeductionInfo] = {}
    sql = """
        SELECT ATC930, AAA901, ATC941
        FROM TC94
        WHERE ATC930 IN ({placeholders})
    """

    with conn.cursor() as cursor:
        for start in range(0, len(atc930_list), _IN_BATCH_SIZE):
            chunk = atc930_list[start:start + _IN_BATCH_SIZE]
            # 占位符由序号生成, 数值全部走绑定, 无用户输入进入 SQL 文本
            placeholders = ", ".join(":b%d" % i for i in range(1, len(chunk) + 1))
            binds = {"b%d" % i: v for i, v in enumerate(chunk, start=1)}
            cursor.execute(sql.format(placeholders=placeholders), binds)
            for row in cursor.fetchall():
                atc930 = int(row[0] or 0)
                aaa901 = int(row[1] or 0)
                amount = float(row[2] or 0)
                info = results.get(atc930)
                if info is None:
                    info = DeductionInfo(atc930=atc930)
                    results[atc930] = info
                field_name = _AAA901_FIELDS.get(aaa901, "其他")
                setattr(info, field_name, getattr(info, field_name) + amount)
    return results


def get_personnel_info(conn, month: int) -> List[PersonnelInfo]:
    """按工资所属年月查询去重人员信息 (TC93 + AC01 左连接)。

    出生日期从身份证第 7-14 位提取 (YYYY-MM-DD); 性别取 AC01.AAC004
    (all_col_comments 证实 AAC004=性别, AAE005 实为联系电话),
    若为数字编码 (1/2) 则转换为 男/女。
    """
    # 注意: 原始需求指定 ac01.AAE005 AS 性别, 但 all_col_comments 证实
    # AAE005 = 联系电话, 真实性别列为 AAC004 (实跑验证: AAE005 返回手机号,
    # AAC004 返回 1/2 性别码)。此处已修正, 避免把电话号写入性别字段。
    sql = """
        SELECT DISTINCT
          ac01.AAC001,
          t93.AAC003 AS 姓名,
          ac01.AAC002 AS 身份证,
          ac01.AAC004 AS 性别
        FROM TC93 t93
        LEFT JOIN AC01 ac01 ON t93.AAC001 = ac01.AAC001
        WHERE t93.ATC931 = :month
        ORDER BY t93.AAC003
    """
    gender_map = {"1": "男", "2": "女"}
    people = []
    with conn.cursor() as cursor:
        cursor.execute(sql, {"month": month})
        for row in cursor.fetchall():
            id_card = str(row[2] or "")
            birthday = ""
            if len(id_card) == 18 and id_card.isdigit():
                birthday = f"{id_card[6:10]}-{id_card[10:12]}-{id_card[12:14]}"
            gender = str(row[3] or "")
            people.append(PersonnelInfo(
                职工号=str(row[0] or ""),          # AC01.AAC001
                姓名=str(row[1] or ""),            # TC93.AAC003
                身份证=id_card,                    # AC01.AAC002
                性别=gender_map.get(gender, gender),  # AC01.AAC004
                出生日期=birthday,
            ))
    return people
