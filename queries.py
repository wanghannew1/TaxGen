"""queries.py - Oracle 11g 工资数据查询层

本模块提供从 Oracle 11g 数据库读取工资数据所需的全部 SQL 查询,
供 Flask 应用生成税务申报 Excel 模板使用。

数据表:
- TC93  工资主表 (~110 万行), 主键 ATC930, ATC931 = 工资所属年月 (YYYYMM)
- TC94  工资扣款明细表 (~220 万行), 通过 ATC930 关联 TC93
- AC01  人员信息表, 通过 AAC001 (个人编号) 关联 TC93

字段映射 (all_col_comments 验证):
- ATC93AA = 本次工资总额
- ATC936 = 本次免税 (= 采暖费ATC93W21 + 独生子女费ATC93W4)
- ATC933 = 本次应发工资
- ATC93C = 本次实发金额(合计)
- ATC93D = 本次个人所得税
- ATC93BD = 大病险（个人承担）
- ATC93BC = 大病险（单位承担）
- ATC93BE = 补缴及退款保险差额（个人）
- ATC93BB = 意外险（单位承担）
- ATC93BH = 意外险（个人承担）
- ATC93BF = 转款合计
- ATC93BG = 补缴及退款个人所得税
- BAA001 = 当月养老个人缴 (8%)
- BAA002 = 当月医疗个人缴 (2%)
- BAA003 = 当月失业个人缴 (0.3%)
- CAA002 = 个人公积金月缴存额 (7%)
- ATC937 = 工资发放次数 (同一结算单元同一月份的发放序号)

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
from decimal import Decimal
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


def get_pay_months(conn) -> List[MonthOption]:
    """查询 TC8M 中所有可用的发放月份(ATC8G7)，供回盘统计选择。"""
    sql = """
        SELECT DISTINCT ATC8G7 FROM TC8M
        WHERE ATC8G7 IS NOT NULL AND ATC8M3 = 2
        ORDER BY ATC8G7 DESC
    """
    options = []
    with conn.cursor() as cursor:
        cursor.execute(sql)
        for row in cursor.fetchall():
            month = int(row[0] or 0)
            if month <= 0:
                continue
            options.append(MonthOption(value=month, label=f"{month // 100}年{month % 100:02d}月"))
    return options


def get_salary_records(conn, month: int) -> List[SalaryRecord]:
    """按工资所属年月查询正常工资记录 (TC93 + AC01 左连接)。

    仅返回 ATC93G='1'(已结算) 的正常记录。
    异常记录(ATC93G≠1 或 NULL)需通过 get_abnormal_records 单独查询。
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
          t93.ATB930 AS 结算单元,
          t93.ATC937 AS 工资发放次数,
          t93.ATC93W2 AS 加班费,
          t93.ATC93W3 AS 餐补,
          t93.ATC93W9 AS 岗位工资,
          t93.ATC93W10 AS 绩效奖金,
          t93.BAA001 AS 养老个人,
          t93.BAA002 AS 医疗个人,
          t93.BAA003 AS 失业个人,
          t93.CAA002 AS 公积金个人,
          t93.ATC93BE AS 补缴及退款保险差额个人,
          t93.ATC93BD AS 大病险个人,
          t93.ATC936 AS 本次免税,
          t93.ATC93AG AS 个人其他调整,
          t93.ATC93E AS 个人欠款,
          t93.ATC93Y2 AS 扣款大病险,
          t93.ATC93Z2 AS 税后工会会费,
          t93.BAA300 AS 个人代理费,
          t93.ATC93BH AS 意外险个人,
          t93.ATC930 AS tc930_id
        FROM TC93 t93
        LEFT JOIN AC01 ac01 ON t93.AAC001 = ac01.AAC001
        WHERE t93.ATC931 = :month
          AND t93.ATC93G = '1'
        ORDER BY t93.AAC003
    """
    records = []
    with conn.cursor() as cursor:
        cursor.execute(sql, {"month": month})
        for row in cursor.fetchall():
            records.append(SalaryRecord(
                职工号=str(row[0] or ""),
                姓名=str(row[1] or ""),
                身份证=str(row[2] or ""),
                工资所属年月=int(row[3] or 0),
                结算单元=int(row[12] or 0),
                当月批次=str(row[13] or ""),
                应发工资=Decimal(str(row[5] or 0)),
                实发工资=Decimal(str(row[6] or 0)),
                个人所得税=Decimal(str(row[7] or 0)),
                工资总额=Decimal(str(row[8] or 0)),
                独生子女费=Decimal(str(row[9] or 0)),
                采暖费=Decimal(str(row[10] or 0)),
                奖金=Decimal(str(row[11] or 0)),
                养老个人=Decimal(str(row[18] or 0)),
                医疗个人=Decimal(str(row[19] or 0)),
                失业个人=Decimal(str(row[20] or 0)),
                公积金个人=Decimal(str(row[21] or 0)),
                补缴及退款保险金额个人=Decimal(str(row[22] or 0)),
                大病险个人=Decimal(str(row[23] or 0)),
                补发3=Decimal(str(row[24] or 0)),
                个人其他调整=Decimal(str(row[25] or 0)),
                个人欠款=Decimal(str(row[26] or 0)),
                扣款大病险=Decimal(str(row[27] or 0)),
                税后工会会费=Decimal(str(row[28] or 0)),
                个人代理费=Decimal(str(row[29] or 0)),
                意外险个人=Decimal(str(row[30] or 0)),
                tc930_id=int(row[31] or 0),
            ))
    return records


def get_abnormal_records(conn, month: int) -> List[dict]:
    """查询因状态字段异常被过滤的TC93记录 (ATC93G≠1 或 NULL)。"""
    sql = """
        SELECT t93.*, ac01.AAC002 AS 身份证
        FROM TC93 t93
        LEFT JOIN AC01 ac01 ON t93.AAC001 = ac01.AAC001
        WHERE t93.ATC931 = :month
          AND (t93.ATC93G != '1' OR t93.ATC93G IS NULL)
        ORDER BY t93.AAC003
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, {"month": month})
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def get_tc93_field_comments(conn) -> Dict[str, str]:
    """查询 TC93 全部字段的 Oracle 注释，供总表导出列头说明使用。"""
    comments = {}
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT column_name, comments FROM all_col_comments
            WHERE table_name = 'TC93'
        """)
        for row in cursor.fetchall():
            comments[str(row[0])] = str(row[1] or "")
    comments["身份证"] = "公民身份号码"
    return comments


def get_tc93_all_fields(conn, month: int) -> List[dict]:
    """查询指定月份TC93全部字段，供导出总表使用。"""
    sql = """
        SELECT t93.*, ac01.AAC002 AS 身份证
        FROM TC93 t93
        LEFT JOIN AC01 ac01 ON t93.AAC001 = ac01.AAC001
        WHERE t93.ATC931 = :month
        ORDER BY t93.AAC003
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, {"month": month})
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


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


def get_suggestions(conn, pay_month: int) -> List[dict]:
    sql = """
        SELECT
            m.ATB930 AS 结算单元,
            m.ATB931 AS 结算单元名称,
            m.ATC931 AS 工资所属月,
            m.ATC937 AS 当月批次,
            SUM(m.ATC8M1) AS 发放人数,
            SUM(m.ATC8M2) AS 发放总额
        FROM TC8M m
        WHERE m.ATC8G7 = :pay_month
        AND m.ATC8M3 = 2
        GROUP BY m.ATB930, m.ATB931, m.ATC931, m.ATC937
        ORDER BY m.ATB930, m.ATC931, m.ATC937
    """
    combos = []
    with conn.cursor() as cursor:
        cursor.execute(sql, {"pay_month": pay_month})
        combos = [{
            "unit": int(r[0] or 0),
            "unit_name": str(r[1] or ""),
            "salary_month": int(r[2] or 0),
            "seq": str(r[3] or ""),
            "person_count": int(r[4] or 0),
            "total_income": float(r[5] or 0),
        } for r in cursor.fetchall()]

    return combos


def search_tc8m(conn, unit_name: str = "", salary_month: int = 0,
                pay_month: int = 0, seq: str = "", status: int = -1,
                handler: str = "") -> List[dict]:
    """查询 TC8M 批次数据。status=-1 表示不限状态，默认只返回已确认(status=2)。"""
    conditions = []
    binds = {}

    if status >= 0:
        conditions.append("m.ATC8M3 = :status")
        binds["status"] = status

    if unit_name:
        conditions.append("m.ATB931 LIKE :unit_name")
        binds["unit_name"] = f"%{unit_name}%"
    if salary_month:
        conditions.append("m.ATC931 = :salary_month")
        binds["salary_month"] = salary_month
    if pay_month:
        conditions.append("m.ATC8G7 = :pay_month")
        binds["pay_month"] = pay_month
    if seq:
        conditions.append("m.ATC937 = :seq")
        binds["seq"] = seq
    if handler:
        conditions.append("m.AAE019 LIKE :handler")
        binds["handler"] = f"%{handler}%"

    where = " AND ".join(conditions)
    sql = f"""
        SELECT
            m.ATB930,
            m.ATB931,
            m.ATC931,
            m.ATC937,
            m.ATC8G7,
            m.ATC8M3,
            m.AAE019,
            SUM(m.ATC8M1),
            SUM(m.ATC8M2)
        FROM TC8M m
        WHERE {where}
        GROUP BY m.ATB930, m.ATB931, m.ATC931, m.ATC937, m.ATC8G7, m.ATC8M3, m.AAE019
        ORDER BY m.ATB931, m.ATC931, m.ATC937
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, binds)
        return [{
            "unit": int(r[0] or 0),
            "unit_name": str(r[1] or ""),
            "salary_month": int(r[2] or 0),
            "seq": str(r[3] or ""),
            "pay_month": int(r[4] or 0),
            "status": int(r[5] or 0),
            "handler": str(r[6] or ""),
            "person_count": int(r[7] or 0),
            "total_income": float(r[8] or 0),
        } for r in cursor.fetchall()]


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


def get_merge_warnings(conn, pay_months, combo_set, persons) -> List[dict]:
    """查询选中记录中的人在未选中组合的工资记录。

    范围限定为同一发放月份(pay_months, TC8M.ATC8G7)下的其他组合，
    用于提示用户: 这些人还有工资未纳入本次合并报税。
    persons 分批(每批900)查询避免 ORA-01795。
    """
    if not persons or not pay_months or not combo_set:
        return []
    warnings = []
    combo_expr = "(" + ",".join(
        f"(:u{i}, :m{i}, :s{i})" for i in range(len(combo_set))) + ")"
    combo_binds = {}
    for i, (u, m, s) in enumerate(combo_set):
        combo_binds[f"u{i}"] = u
        combo_binds[f"m{i}"] = m
        combo_binds[f"s{i}"] = s
    pay_expr = ",".join(f":pm{i}" for i in range(len(pay_months)))
    pay_binds = {f"pm{i}": pm for i, pm in enumerate(pay_months)}
    sql_tpl = f"""
        SELECT t93.AAC003, ac01.AAC002, t93.ATB930, t93.ATC931, t93.ATC937, t93.ATC93AA,
               (SELECT MAX(m.ATB931) FROM TC8M m
                WHERE m.ATB930 = t93.ATB930 AND m.ATC931 = t93.ATC931 AND m.ATC937 = t93.ATC937)
        FROM TC93 t93
        LEFT JOIN AC01 ac01 ON t93.AAC001 = ac01.AAC001
        WHERE t93.ATC93G = '1'
          AND NVL(t93.ATC93AA, 0) > 0
          AND (t93.ATB930, t93.ATC931, t93.ATC937) IN (
              SELECT m.ATB930, m.ATC931, m.ATC937 FROM TC8M m
              WHERE m.ATC8G7 IN ({pay_expr}) AND m.ATC8M3 = 2
          )
          AND t93.AAC001 IN ({{placeholders}})
          AND (t93.ATB930, t93.ATC931, t93.ATC937) NOT IN {combo_expr}
        ORDER BY t93.AAC003
    """
    with conn.cursor() as cursor:
        for start in range(0, len(persons), _IN_BATCH_SIZE):
            chunk = persons[start:start + _IN_BATCH_SIZE]
            placeholders = ", ".join(f":p{i}" for i in range(len(chunk)))
            binds = {**combo_binds, **pay_binds}
            binds.update({f"p{i}": p for i, p in enumerate(chunk)})
            cursor.execute(sql_tpl.format(placeholders=placeholders), binds)
            for row in cursor.fetchall():
                warnings.append({
                    "name": str(row[0] or ""),
                    "cert_no": str(row[1] or ""),
                    "unit": int(row[2] or 0),
                    "unit_name": str(row[6] or ""),
                    "salary_month": int(row[3] or 0),
                    "seq": str(row[4] or ""),
                    "income": float(row[5] or 0),
                })
    return warnings
