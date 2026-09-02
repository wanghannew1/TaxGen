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
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Set

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
          t93.ATC93X3 AS 个人交纳现金,
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
                个人交纳现金=Decimal(str(row[25] or 0)),
                个人其他调整=Decimal(str(row[26] or 0)),
                个人欠款=Decimal(str(row[27] or 0)),
                扣款大病险=Decimal(str(row[28] or 0)),
                税后工会会费=Decimal(str(row[29] or 0)),
                个人代理费=Decimal(str(row[30] or 0)),
                意外险个人=Decimal(str(row[31] or 0)),
                tc930_id=int(row[32] or 0),
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
    comments["ATC8G7"] = "经办年月（发放年月最终依据，来自TC8M；与TC93.ATC932工资发放年月、ATC931工资所属年月易混淆）"
    return comments


def get_tc93_all_fields(conn, month: int) -> List[dict]:
    """查询指定月份TC93全部字段，供导出总表使用。附经办年月(ATC8G7)作发放年月最终依据。"""
    sql = """
        SELECT t93.*, ac01.AAC002 AS 身份证,
               (SELECT MAX(m.ATC8G7) FROM TC8M m
                WHERE m.ATB930 = t93.ATB930 AND m.ATC931 = t93.ATC931 AND m.ATC937 = t93.ATC937
                  AND m.ATC8M3 = 2) AS ATC8G7
        FROM TC93 t93
        LEFT JOIN AC01 ac01 ON t93.AAC001 = ac01.AAC001
        WHERE t93.ATC931 = :month
        ORDER BY ac01.AAC002 NULLS LAST, t93.ATC930
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
                WHERE m.ATB930 = t93.ATB930 AND m.ATC931 = t93.ATC931 AND m.ATC937 = t93.ATC937),
               (SELECT MAX(m.ATC8G7) FROM TC8M m
                WHERE m.ATB930 = t93.ATB930 AND m.ATC931 = t93.ATC931 AND m.ATC937 = t93.ATC937
                  AND m.ATC8M3 = 2)
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
                    "pay_month": int(row[7] or 0),
                    "seq": str(row[4] or ""),
                    "income": float(row[5] or 0),
                })
    return warnings


def get_latest_pay_date(conn):
    """查询最近一笔工资发放的经办日期 (TC8M.AAE036, 已发放状态)。

    返回 (pay_month: int, pay_date: datetime), 无记录时返回 (0, None)。
    """
    sql = """
        SELECT MAX(ATC8G7), MAX(AAE036)
        FROM TC8M
        WHERE ATC8M3 = 2 AND ATC8G7 IS NOT NULL
    """
    with conn.cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
        if not row or row[0] is None:
            return 0, None
        return int(row[0]), row[1]


def get_payroll_cert_numbers(conn, pay_month: int) -> Set[str]:
    """按发放月份(经办年月)查询全部发薪人员的身份证号集合。

    发放月份 = TC8M.ATC8G7 (经办年月), 且 ATC8M3='2'(已发放);
    TC93.ATC93G='1' 表示已结算。通过 (ATB930, ATC931, ATC937)
    三元组关联 TC93 与 TC8M, 左连 AC01 取身份证号。
    返回统一大写的证件号码集合(处理末位 X), 用于增减员比对。
    """
    sql = """
        SELECT DISTINCT ac01.AAC002
        FROM TC93 t93
        JOIN TC8M m ON m.ATB930 = t93.ATB930
                   AND m.ATC931 = t93.ATC931
                   AND m.ATC937 = t93.ATC937
        LEFT JOIN AC01 ac01 ON t93.AAC001 = ac01.AAC001
        WHERE m.ATC8G7 = :pay_month
          AND m.ATC8M3 = 2
          AND t93.ATC93G = '1'
          AND ac01.AAC002 IS NOT NULL
    """
    certs = set()
    with conn.cursor() as cursor:
        cursor.execute(sql, {"pay_month": pay_month})
        for row in cursor.fetchall():
            cert = str(row[0] or "").strip().upper()
            if cert:
                certs.add(cert)
    return certs


def get_tc90_termination_dates(conn, cert_numbers) -> Dict[str, datetime]:
    """按身份证号批量查询 TC90 合同终止日期 (ATC90D)。

    同一证件号在 TC90 可能有多个历史合同记录, 取 MAX(ATC90D)
    作为最新合同终止日期。IN 列表分批查询避免 ORA-01795。
    返回 {证件号(大写): datetime}, 无合同终止日期的证件号不出现。
    """
    from datetime import datetime
    if not cert_numbers:
        return {}
    dates: Dict[str, datetime] = {}
    sql = """
        SELECT AAC002, MAX(ATC90D)
        FROM TC90
        WHERE AAC002 IN ({placeholders})
          AND ATC90D IS NOT NULL
        GROUP BY AAC002
    """
    cert_list = list(cert_numbers)
    with conn.cursor() as cursor:
        for start in range(0, len(cert_list), _IN_BATCH_SIZE):
            chunk = cert_list[start:start + _IN_BATCH_SIZE]
            placeholders = ", ".join(f":b{i}" for i in range(len(chunk)))
            binds = {f"b{i}": c for i, c in enumerate(chunk)}
            cursor.execute(sql.format(placeholders=placeholders), binds)
            for row in cursor.fetchall():
                cert = str(row[0] or "").strip().upper()
                if cert and row[1] is not None:
                    dates[cert] = row[1]
    return dates


def get_payroll_personnel(conn, pay_month: int) -> List[PersonnelInfo]:
    """按发放月份(经办年月)查询全部发薪人员详细信息, 用于增员模板。

    与 get_payroll_cert_numbers 相同的 TC8M 关联口径, 按个人编号去重,
    手机号/出生日期取 MAX 避免同人多行。性别码 1/2 转换为 男/女。
    任职受雇从业日期取 TC90.ATC90C 合同开始日期 (多行历史合同时取最早)。
    按证件号分组取 MAX(工号) 避免同人多行 (同一证件号可能有历史工号)。
    """
    sql = """
        SELECT
          MAX(ac01.AAC001) AS 工号,
          MAX(ac01.AAC003) AS 姓名,
          MAX(ac01.AAC002) AS 身份证,
          MAX(ac01.AAC004) AS 性别,
          MAX(ac01.AAC006) AS 出生日期,
          MAX(ac01.AAE005) AS 联系电话,
          MIN(t90.ATC90C) AS 任职受雇从业日期,
          MAX(m.ATB931) AS 结算单元名称
        FROM TC93 t93
        JOIN TC8M m ON m.ATB930 = t93.ATB930
                   AND m.ATC931 = t93.ATC931
                   AND m.ATC937 = t93.ATC937
        LEFT JOIN AC01 ac01 ON t93.AAC001 = ac01.AAC001
        LEFT JOIN TC90 t90 ON t90.AAC002 = ac01.AAC002
        WHERE m.ATC8G7 = :pay_month
          AND m.ATC8M3 = 2
          AND t93.ATC93G = '1'
          AND ac01.AAC002 IS NOT NULL
        GROUP BY ac01.AAC002
        ORDER BY MAX(ac01.AAC003)
    """
    gender_map = {"1": "男", "2": "女"}
    people = []
    with conn.cursor() as cursor:
        cursor.execute(sql, {"pay_month": pay_month})
        for row in cursor.fetchall():
            id_card = str(row[2] or "").strip().upper()
            birthday = ""
            if row[4] is not None:
                b = row[4]
                if hasattr(b, "strftime"):
                    birthday = b.strftime("%Y-%m-%d")
                else:
                    birthday = str(b)
            elif len(id_card) == 18:
                birthday = f"{id_card[6:10]}-{id_card[10:12]}-{id_card[12:14]}"
            gender = str(row[3] or "")
            start_date = ""
            if row[6] is not None:
                s = row[6]
                if hasattr(s, "strftime"):
                    start_date = s.strftime("%Y-%m-%d")
                else:
                    start_date = str(s)
            people.append(PersonnelInfo(
                职工号=str(row[0] or ""),
                姓名=str(row[1] or ""),
                身份证=id_card,
                性别=gender_map.get(gender, gender),
                出生日期=birthday,
                手机号码=str(row[5] or ""),
                任职受雇从业日期=start_date,
                备注=str(row[7] or ""),
            ))
    return people


def get_unpaid_salary_persons(conn, salary_months) -> Set[str]:
    """查询指定所属月份范围内"已做工资单但未发薪"的人员证件号集合。

    口径: TC93 有工资记录, 但 TC8M 无对应已发记录 (ATC8M3=2) 的人员。
    返回统一大写的证件号码集合, 用于减员排除/增员判断。
    """
    if not salary_months:
        return set()
    certs = set()
    sql = """
        SELECT DISTINCT ac01.AAC002
        FROM TC93 t93
        LEFT JOIN TC8M m ON m.ATB930 = t93.ATB930
                        AND m.ATC931 = t93.ATC931
                        AND m.ATC937 = t93.ATC937
                        AND m.ATC8M3 = 2
        LEFT JOIN AC01 ac01 ON t93.AAC001 = ac01.AAC001
        WHERE t93.ATC931 IN ({placeholders})
          AND t93.ATC93G = '1'
          AND m.ATB930 IS NULL
          AND ac01.AAC002 IS NOT NULL
    """
    months = sorted(set(salary_months))
    with conn.cursor() as cursor:
        for start in range(0, len(months), _IN_BATCH_SIZE):
            chunk = months[start:start + _IN_BATCH_SIZE]
            placeholders = ", ".join(f":m{i}" for i in range(len(chunk)))
            binds = {f"m{i}": m for i, m in enumerate(chunk)}
            cursor.execute(sql.format(placeholders=placeholders), binds)
            for row in cursor.fetchall():
                cert = str(row[0] or "").strip().upper()
                if cert:
                    certs.add(cert)
    return certs


def get_pay_month_range(selected_month: int, conn) -> List[int]:
    """计算发薪月份筛选范围: 选中月份 → 最近发薪月份。

    最近发薪月份 = TC8M 最新 ATC8G7 (ATC8M3=2)。
    选中月份大于等于最近发薪月份时只返回选中月份。
    """
    latest = 0
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT MAX(ATC8G7) FROM TC8M WHERE ATC8M3 = 2 AND ATC8G7 IS NOT NULL")
        row = cursor.fetchone()
        latest = int(row[0] or 0) if row else 0
    if latest and selected_month < latest:
        months = []
        y, m = selected_month // 100, selected_month % 100
        while selected_month <= latest:
            months.append(selected_month)
            m += 1
            if m > 12:
                y += 1
                m = 1
            selected_month = y * 100 + m
        return months
    return [selected_month]


def get_unpaid_month_range(selected_month: int) -> List[int]:
    """计算未发薪工资表所属月份范围: 选中月份 → 当前系统月份。

    结束月份取当前系统时间所在月 (如今天 2026-09 → 结束 202609)。
    """
    from datetime import datetime
    now = datetime.now()
    current_month = now.year * 100 + now.month
    if selected_month >= current_month:
        return [selected_month]
    months = []
    y, m = selected_month // 100, selected_month % 100
    cur = selected_month
    while cur <= current_month:
        months.append(cur)
        m += 1
        if m > 12:
            y += 1
            m = 1
        cur = y * 100 + m
    return months


def get_contract_date_range(selected_month: int, report_month: int):
    """计算合同签署时间筛选范围。

    开始日期: 选中月份当月1日 (如 202607 → 2026-07-01)
    结束日期: 上报月份最后一天 (如 202608 → 2026-08-31)
    返回 (start_date, end_date) 字符串 "YYYY-MM-DD"。
    """
    import calendar
    sy, sm = selected_month // 100, selected_month % 100
    ry, rm = report_month // 100, report_month % 100
    start_date = f"{sy:04d}-{sm:02d}-01"
    last_day = calendar.monthrange(ry, rm)[1]
    end_date = f"{ry:04d}-{rm:02d}-{last_day:02d}"
    return start_date, end_date


def get_contract_signed_persons(conn, start_date: str, end_date: str) -> Set[str]:
    """查询合同开始日期 (TC90.ATC90C) 在指定范围内的人员证件号集合。

    返回统一大写的证件号码集合。
    """
    certs = set()
    sql = """
        SELECT DISTINCT AAC002
        FROM TC90
        WHERE ATC90C >= TO_DATE(:start_date, 'YYYY-MM-DD')
          AND ATC90C <= TO_DATE(:end_date, 'YYYY-MM-DD')
          AND AAC002 IS NOT NULL
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, {"start_date": start_date, "end_date": end_date})
        for row in cursor.fetchall():
            cert = str(row[0] or "").strip().upper()
            if cert:
                certs.add(cert)
    return certs


def get_personnel_by_certs(conn, cert_numbers) -> List[PersonnelInfo]:
    """按证件号集合批量查询人员详细信息 (AC01 为主, TC90 补充任职日期)。

    用于为增员人员(未发薪/合同签署)补充 51 列所需信息。
    按证件号分组取 MAX(工号) 避免同人多行 (同一证件号可能有历史工号)。
    返回 PersonnelInfo 列表, 任职受雇从业日期取 TC90.ATC90C 最早。
    """
    if not cert_numbers:
        return []
    certs = sorted({str(c).strip().upper() for c in cert_numbers if str(c).strip()})
    sql = """
        SELECT
          MAX(ac01.AAC001) AS 工号,
          MAX(ac01.AAC003) AS 姓名,
          MAX(ac01.AAC002) AS 身份证,
          MAX(ac01.AAC004) AS 性别,
          MAX(ac01.AAC006) AS 出生日期,
          MAX(ac01.AAE005) AS 联系电话,
          MIN(t90.ATC90C) AS 任职日期
        FROM AC01 ac01
        LEFT JOIN TC90 t90 ON t90.AAC002 = ac01.AAC002
        WHERE ac01.AAC002 IN ({placeholders})
        GROUP BY ac01.AAC002
    """
    gender_map = {"1": "男", "2": "女"}
    people = []
    with conn.cursor() as cursor:
        for start in range(0, len(certs), _IN_BATCH_SIZE):
            chunk = certs[start:start + _IN_BATCH_SIZE]
            placeholders = ", ".join(f":c{i}" for i in range(len(chunk)))
            binds = {f"c{i}": c for i, c in enumerate(chunk)}
            cursor.execute(sql.format(placeholders=placeholders), binds)
            for row in cursor.fetchall():
                id_card = str(row[2] or "").strip().upper()
                birthday = ""
                if row[4] is not None:
                    b = row[4]
                    birthday = b.strftime("%Y-%m-%d") if hasattr(b, "strftime") else str(b)
                start_date = ""
                if row[6] is not None:
                    s = row[6]
                    start_date = s.strftime("%Y-%m-%d") if hasattr(s, "strftime") else str(s)
                gender = str(row[3] or "")
                people.append(PersonnelInfo(
                    职工号=str(row[0] or ""),
                    姓名=str(row[1] or ""),
                    身份证=id_card,
                    性别=gender_map.get(gender, gender),
                    出生日期=birthday,
                    手机号码=str(row[5] or ""),
                    任职受雇从业日期=start_date,
                ))
    return people


def get_default_report_month(conn) -> int:
    """计算默认上报发薪月份。

    规则: 系统时间 1-15日 → 上个月; 16日及以后 → 本月 (若已有发薪记录则返回本月,
    否则回退到最近发薪月份)。报税一般在发薪月过完后半个月内进行。
    """
    from datetime import datetime
    now = datetime.now()
    if now.day <= 15:
        last_month = now.replace(day=1)
        from datetime import timedelta
        last_month -= timedelta(days=1)
        default = last_month.year * 100 + last_month.month
    else:
        default = now.year * 100 + now.month
    # 校验默认月份是否已有发薪记录, 无则回退到最近发薪月份
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT MAX(ATC8G7) FROM TC8M WHERE ATC8M3 = 2 AND ATC8G7 IS NOT NULL")
        row = cursor.fetchone()
        latest = int(row[0] or 0) if row else 0
    if latest and default > latest:
        return latest
    return default


def get_handlers(conn, pay_month: int = 0) -> List[str]:
    """查询经办人列表 (TC8M.AAE019 经办人, 已发放状态)。

    注意: Oracle 中空字符串等价于 NULL, 只需 IS NOT NULL 判断。
    pay_month 为 0 时查询全部经办人, 否则限定该发薪月份。
    """
    sql = """
        SELECT DISTINCT AAE019 FROM TC8M
        WHERE AAE019 IS NOT NULL
          AND ATC8M3 = 2
          {month_cond}
        ORDER BY AAE019
    """
    month_cond = "AND ATC8G7 = :pay_month" if pay_month else ""
    with conn.cursor() as cursor:
        if pay_month:
            cursor.execute(sql.format(month_cond=month_cond), {"pay_month": pay_month})
        else:
            cursor.execute(sql.format(month_cond=month_cond))
        return [str(r[0]) for r in cursor.fetchall()]


def get_units(conn, pay_month: int = 0) -> List[dict]:
    """查询结算单元列表 (TB93 结算单元信息表全量; 指定月份时限定 TC8M 已发放)。"""
    if pay_month:
        sql = """
            SELECT DISTINCT ATB930, ATB931 FROM TC8M
            WHERE ATB930 IS NOT NULL AND ATC8M3 = 2 AND ATC8G7 = :pay_month
            ORDER BY ATB930
        """
        with conn.cursor() as cursor:
            cursor.execute(sql, {"pay_month": pay_month})
            return [{"code": int(r[0]), "name": str(r[1] or "")} for r in cursor.fetchall()]
    sql = """
        SELECT DISTINCT ATB930, ATB931 FROM TB93
        WHERE ATB930 IS NOT NULL AND ATB931 IS NOT NULL
        ORDER BY ATB930
    """
    with conn.cursor() as cursor:
        cursor.execute(sql)
        return [{"code": int(r[0]), "name": str(r[1] or "")} for r in cursor.fetchall()]


def get_person_units(conn, cert_numbers, pay_months) -> Dict[str, dict]:
    """批量查询人员的经办人/结算单元/单位信息。

    通过 TC93(工资) JOIN TC8M(经办) JOIN AC01(人员) 关联:
    - 结算单元名称/代码取 TC93.ATB931/ATB930 (未发薪/发薪记录都有)
    - 单位名称取 AC01.AAB004 (人员表直接关联)
    - 经办人取 TC8M.AAE019 (已发放状态, 仅发薪人员有)
    返回 {证件号(大写): {"handler": 经办人, "unit_code": 结算单元代码,
    "unit_name": 结算单元名称(ATB931), "dept_name": 单位名称(AAB004)}}。
    """
    if not cert_numbers:
        return {}
    certs = sorted({str(c).strip().upper() for c in cert_numbers if str(c).strip()})
    result: Dict[str, dict] = {}
    sql = """
        SELECT ac01.AAC002, MAX(m.AAE019) AS handler,
               MAX(t93.ATB930) AS unit_code, MAX(t93.ATB931) AS unit_name,
               MAX(t93.AAB004) AS dept_name
        FROM TC93 t93
        LEFT JOIN TC8M m ON m.ATB930 = t93.ATB930
                        AND m.ATC931 = t93.ATC931
                        AND m.ATC937 = t93.ATC937
                        AND m.ATC8M3 = 2
                        AND m.ATC8G7 IN ({month_placeholders})
        LEFT JOIN AC01 ac01 ON t93.AAC001 = ac01.AAC001
        WHERE t93.ATC93G = '1'
          AND ac01.AAC002 IN ({cert_placeholders})
        GROUP BY ac01.AAC002
    """
    months = sorted(set(pay_months)) if pay_months else [0]
    with conn.cursor() as cursor:
        for start in range(0, len(certs), _IN_BATCH_SIZE):
            chunk = certs[start:start + _IN_BATCH_SIZE]
            month_ph = ", ".join(f":pm{i}" for i in range(len(months)))
            cert_ph = ", ".join(f":c{i}" for i in range(len(chunk)))
            binds = {f"pm{i}": m for i, m in enumerate(months)}
            binds.update({f"c{i}": c for i, c in enumerate(chunk)})
            cursor.execute(sql.format(month_placeholders=month_ph,
                                      cert_placeholders=cert_ph), binds)
            for row in cursor.fetchall():
                cert = str(row[0] or "").strip().upper()
                if cert:
                    result[cert] = {
                        "handler": str(row[1] or ""),
                        "unit_code": int(row[2] or 0),
                        "unit_name": str(row[3] or ""),
                        "dept_name": str(row[4] or ""),
                    }
    return result


def get_zero_salary_certs(conn, pay_month: int, unit_codes: List[int]) -> Set[str]:
    """查询指定发薪月份合计工资为0的结算单元中的人员证件号集合。

    用途: 特殊结算单元(如二院)当月多批次合计工资为0时, 这些人员不增员。
    unit_codes: 从 config_db 读取的 "工资为0不增员不报税" 结算单元代码列表
                (config 数据存 SQLite, 不读取 Oracle 配置表)。
    """
    if not unit_codes:
        return set()
    certs = set()
    sql = """
        SELECT DISTINCT ac01.AAC002
        FROM TC93 t93
        JOIN TC8M m ON m.ATB930 = t93.ATB930
                   AND m.ATC931 = t93.ATC931
                   AND m.ATC937 = t93.ATC937
        LEFT JOIN AC01 ac01 ON t93.AAC001 = ac01.AAC001
        WHERE m.ATC8G7 = :pay_month
          AND m.ATC8M3 = 2
          AND t93.ATC93G = '1'
          AND ac01.AAC002 IS NOT NULL
          AND t93.ATB930 IN ({placeholders})
          AND NVL(t93.ATC93AA, 0) = 0
    """
    codes = sorted(set(unit_codes))
    with conn.cursor() as cursor:
        for start in range(0, len(codes), _IN_BATCH_SIZE):
            chunk = codes[start:start + _IN_BATCH_SIZE]
            placeholders = ", ".join(f":u{i}" for i in range(len(chunk)))
            binds = {"pay_month": pay_month}
            binds.update({f"u{i}": c for i, c in enumerate(chunk)})
            cursor.execute(sql.format(placeholders=placeholders), binds)
            for row in cursor.fetchall():
                cert = str(row[0] or "").strip().upper()
                if cert:
                    certs.add(cert)
    return certs


def get_excluded_unit_certs(conn, pay_months, unit_codes: List[int],
                            relevant_months: List[int] = None) -> Set[str]:
    """查询完全排除单位 (exclude_all=1) 中应排除的人员证件号。

    规则: 仅在排除结算单元有记录的人员才排除 (该单位不论是否发工资都不增员)。
    例外: 若员工在当期 (relevant_months 范围内) 同时有排除单位与未排除单位记录,
          保留增员 (排除单位部分不报税, 未排除单位部分正常报税)。
    历史记录 (非当期) 不构成跨单位例外。
    范围: TC93 工资记录 (当期) ∪ TC90 合同记录。
    unit_codes: 从 config_db 读取的 "完全排除不增员不报税" 结算单元代码列表。
    """
    if not pay_months or not unit_codes:
        return set()
    # 跨单位例外只认当期相关月份 (发薪月份+未发薪月份范围)
    months = sorted(set(relevant_months or pay_months))
    month_ph = ", ".join(f":m{i}" for i in range(len(months)))
    month_binds = {f"m{i}": m for i, m in enumerate(months)}
    certs = set()
    codes = sorted(set(unit_codes))
    sql = f"""
        SELECT DISTINCT ac01.AAC002
        FROM TC93 t93
        LEFT JOIN AC01 ac01 ON t93.AAC001 = ac01.AAC001
        WHERE t93.ATC93G = '1'
          AND ac01.AAC002 IS NOT NULL
          AND t93.ATB930 IN ({{ph1}})
          AND NOT EXISTS (
              SELECT 1 FROM TC93 t93b
              LEFT JOIN AC01 ac01b ON t93b.AAC001 = ac01b.AAC001
              WHERE t93b.ATC93G = '1'
                AND ac01b.AAC002 = ac01.AAC002
                AND t93b.ATC931 IN ({month_ph})
                AND t93b.ATB930 NOT IN ({{ph2}})
          )
    """
    with conn.cursor() as cursor:
        for start in range(0, len(codes), _IN_BATCH_SIZE):
            chunk = codes[start:start + _IN_BATCH_SIZE]
            ph1 = ", ".join(f":a{i}" for i in range(len(chunk)))
            ph2 = ", ".join(f":b{i}" for i in range(len(chunk)))
            binds = {f"a{i}": c for i, c in enumerate(chunk)}
            binds.update({f"b{i}": c for i, c in enumerate(chunk)})
            binds.update(month_binds)
            cursor.execute(sql.format(ph1=ph1, ph2=ph2), binds)
            for row in cursor.fetchall():
                cert = str(row[0] or "").strip().upper()
                if cert:
                    certs.add(cert)
    # 补充 TC90 合同单位 (无工资记录但有合同), 同样应用跨单位例外 (按合同起止时间过滤)
    sql90 = f"""
        SELECT DISTINCT t90.AAC002
        FROM TC90 t90
        WHERE t90.AAC002 IS NOT NULL
          AND t90.ATB930 IN ({{ph1}})
          AND NOT EXISTS (
              SELECT 1 FROM TC90 t90b
              WHERE t90b.AAC002 = t90.AAC002
                AND t90b.ATB930 NOT IN ({{ph2}})
                AND t90b.ATC90C IS NOT NULL
                AND t90b.ATC90D IS NOT NULL
                AND NOT (t90b.ATC90D < TO_DATE(:min_m, 'YYYYMMDD') OR t90b.ATC90C > TO_DATE(:max_m, 'YYYYMMDD'))
          )
    """
    import calendar
    min_m = f"{min(months)}01"
    last_day = calendar.monthrange(max(months) // 100, max(months) % 100)[1]
    max_m = f"{max(months)}{last_day:02d}"
    with conn.cursor() as cursor:
        for start in range(0, len(codes), _IN_BATCH_SIZE):
            chunk = codes[start:start + _IN_BATCH_SIZE]
            ph1 = ", ".join(f":a{i}" for i in range(len(chunk)))
            ph2 = ", ".join(f":b{i}" for i in range(len(chunk)))
            binds = {f"a{i}": c for i, c in enumerate(chunk)}
            binds.update({f"b{i}": c for i, c in enumerate(chunk)})
            binds.update({"min_m": min_m, "max_m": max_m})
            cursor.execute(sql90.format(ph1=ph1, ph2=ph2), binds)
            for row in cursor.fetchall():
                cert = str(row[0] or "").strip().upper()
                if cert:
                    certs.add(cert)
    return certs


def get_person_units_contract(conn, cert_numbers) -> Dict[str, dict]:
    """为仅合同人员(无工资记录)从 TC90 补充结算单元/单位信息。

    结算单元名称: TC90.ATC90X (TC90 自带结算单元名称, 无需关联 TC8M)
    单位名称: TC90.AAB004 (降级用)
    返回 {证件号(大写): {"unit_code", "unit_name", "dept_name"}}。
    """
    if not cert_numbers:
        return {}
    certs = sorted({str(c).strip().upper() for c in cert_numbers if str(c).strip()})
    result: Dict[str, dict] = {}
    sql = """
        SELECT t90.AAC002, MAX(t90.ATB930) AS unit_code,
               MAX(t90.ATC90X) AS unit_name, MAX(t90.AAB004) AS dept_name
        FROM TC90 t90
        WHERE t90.AAC002 IN ({placeholders})
        GROUP BY t90.AAC002
    """
    with conn.cursor() as cursor:
        for start in range(0, len(certs), _IN_BATCH_SIZE):
            chunk = certs[start:start + _IN_BATCH_SIZE]
            placeholders = ", ".join(f":c{i}" for i in range(len(chunk)))
            binds = {f"c{i}": c for i, c in enumerate(chunk)}
            cursor.execute(sql.format(placeholders=placeholders), binds)
            for row in cursor.fetchall():
                cert = str(row[0] or "").strip().upper()
                if cert:
                    result[cert] = {
                        "unit_code": int(row[1] or 0),
                        "unit_name": str(row[2] or ""),
                        "dept_name": str(row[3] or ""),
                    }
    return result


def get_depts(conn, pay_month: int = 0) -> List[str]:
    """查询单位名称列表 (TC93.AAB004, 已结算状态)。

    pay_month 为 0 时查询全部单位, 否则限定该发薪月份。
    """
    sql = """
        SELECT DISTINCT t93.AAB004 FROM TC93 t93
        JOIN TC8M m ON m.ATB930 = t93.ATB930
                   AND m.ATC931 = t93.ATC931
                   AND m.ATC937 = t93.ATC937
        WHERE t93.AAB004 IS NOT NULL
          AND t93.ATC93G = '1'
          AND m.ATC8M3 = 2
          {month_cond}
        ORDER BY t93.AAB004
    """
    month_cond = "AND m.ATC8G7 = :pay_month" if pay_month else ""
    with conn.cursor() as cursor:
        if pay_month:
            cursor.execute(sql.format(month_cond=month_cond), {"pay_month": pay_month})
        else:
            cursor.execute(sql.format(month_cond=month_cond))
        return [str(r[0]) for r in cursor.fetchall()]


def get_salary_details(conn, cert_numbers, salary_months) -> List[dict]:
    """按证件号+所属月份批量查询工资明细 (TC93 已结算记录)。

    返回每条记录: {cert, 姓名, 结算单元, 结算单元名称, 所属月份, 批次,
    应发工资, 本期收入, 养老, 医疗, 失业, 公积金}。
    本期收入 = 应发 - 补缴及退款保险 - 大病险 - 采暖费 - 独生子女费。
    """
    if not cert_numbers:
        return []
    certs = sorted({str(c).strip().upper() for c in cert_numbers if str(c).strip()})
    months = sorted(set(salary_months)) if salary_months else [0]
    results = []
    sql = """
        SELECT ac01.AAC002, t93.AAC003, t93.ATB930, t93.ATB931,
               t93.ATC931, t93.ATC937,
               NVL(t93.ATC933,0), NVL(t93.ATC93BE,0), NVL(t93.ATC93BD,0),
               NVL(t93.ATC93W21,0), NVL(t93.ATC93W4,0),
               NVL(t93.BAA001,0), NVL(t93.BAA002,0),
               NVL(t93.BAA003,0), NVL(t93.CAA002,0),
               t93.AAE019
        FROM TC93 t93
        LEFT JOIN AC01 ac01 ON t93.AAC001 = ac01.AAC001
        WHERE t93.ATC93G = '1'
          AND t93.ATC931 IN ({month_placeholders})
          AND ac01.AAC002 IN ({cert_placeholders})
        ORDER BY ac01.AAC002, t93.ATC931, t93.ATC937
    """
    with conn.cursor() as cursor:
        for start in range(0, len(certs), _IN_BATCH_SIZE):
            chunk = certs[start:start + _IN_BATCH_SIZE]
            month_ph = ", ".join(f":pm{i}" for i in range(len(months)))
            cert_ph = ", ".join(f":c{i}" for i in range(len(chunk)))
            binds = {f"pm{i}": m for i, m in enumerate(months)}
            binds.update({f"c{i}": c for i, c in enumerate(chunk)})
            cursor.execute(sql.format(month_placeholders=month_ph,
                                      cert_placeholders=cert_ph), binds)
            for row in cursor.fetchall():
                income = (float(row[6] or 0) - float(row[7] or 0) - float(row[8] or 0)
                          - float(row[9] or 0) - float(row[10] or 0))
                results.append({
                    "cert": str(row[0] or "").strip().upper(),
                    "姓名": str(row[1] or ""),
                    "unit_code": int(row[2] or 0),
                    "unit_name": str(row[3] or ""),
                    "salary_month": int(row[4] or 0),
                    "seq": str(row[5] or ""),
                    "应发工资": round(float(row[6] or 0), 2),
                    "本期收入": round(income, 2),
                    "养老": round(float(row[11] or 0), 2),
                    "医疗": round(float(row[12] or 0), 2),
                    "失业": round(float(row[13] or 0), 2),
                    "公积金": round(float(row[14] or 0), 2),
                    "handler": str(row[15] or ""),
                })
    return results


def get_tc8m_records(conn, cert_numbers, pay_months) -> List[dict]:
    """按证件号+发薪月份批量查询发放记录 (TC8M 已发放)。

    返回每条记录: {cert, 姓名, 结算单元, 结算单元名称, 发放月份, 所属月份, 批次, 经办人}。
    """
    if not cert_numbers:
        return []
    certs = sorted({str(c).strip().upper() for c in cert_numbers if str(c).strip()})
    months = sorted(set(pay_months)) if pay_months else [0]
    results = []
    sql = """
        SELECT ac01.AAC002, t93.AAC003, m.ATB930, m.ATB931,
               m.ATC8G7, m.ATC931, m.ATC937, m.AAE019
        FROM TC8M m
        JOIN TC93 t93 ON t93.ATB930 = m.ATB930
                     AND t93.ATC931 = m.ATC931
                     AND t93.ATC937 = m.ATC937
        LEFT JOIN AC01 ac01 ON t93.AAC001 = ac01.AAC001
        WHERE m.ATC8M3 = 2
          AND m.ATC8G7 IN ({month_placeholders})
          AND ac01.AAC002 IN ({cert_placeholders})
        ORDER BY ac01.AAC002, m.ATC8G7
    """
    with conn.cursor() as cursor:
        for start in range(0, len(certs), _IN_BATCH_SIZE):
            chunk = certs[start:start + _IN_BATCH_SIZE]
            month_ph = ", ".join(f":pm{i}" for i in range(len(months)))
            cert_ph = ", ".join(f":c{i}" for i in range(len(chunk)))
            binds = {f"pm{i}": m for i, m in enumerate(months)}
            binds.update({f"c{i}": c for i, c in enumerate(chunk)})
            cursor.execute(sql.format(month_placeholders=month_ph,
                                      cert_placeholders=cert_ph), binds)
            for row in cursor.fetchall():
                results.append({
                    "cert": str(row[0] or "").strip().upper(),
                    "姓名": str(row[1] or ""),
                    "unit_code": int(row[2] or 0),
                    "unit_name": str(row[3] or ""),
                    "pay_month": int(row[4] or 0),
                    "salary_month": int(row[5] or 0),
                    "seq": str(row[6] or ""),
                    "handler": str(row[7] or ""),
                })
    return results


def get_tc90_records(conn, cert_numbers) -> List[dict]:
    """按证件号批量查询合同记录 (TC90)。

    返回每条记录: {cert, 姓名, 结算单元, 结算单元名称, 合同开始日期, 合同终止日期,
    单位名称, 经办人}。
    """
    if not cert_numbers:
        return []
    certs = sorted({str(c).strip().upper() for c in cert_numbers if str(c).strip()})
    results = []
    sql = """
        SELECT t90.AAC002, t90.AAC003, t90.ATB930, m.ATB931,
               t90.ATC90C, t90.ATC90D, t90.AAB004, t90.AAE019
        FROM TC90 t90
        LEFT JOIN TC8M m ON m.ATB930 = t90.ATB930
        WHERE t90.AAC002 IN ({placeholders})
        ORDER BY t90.AAC002, t90.ATC90C
    """
    with conn.cursor() as cursor:
        for start in range(0, len(certs), _IN_BATCH_SIZE):
            chunk = certs[start:start + _IN_BATCH_SIZE]
            placeholders = ", ".join(f":c{i}" for i in range(len(chunk)))
            binds = {f"c{i}": c for i, c in enumerate(chunk)}
            cursor.execute(sql.format(placeholders=placeholders), binds)
            for row in cursor.fetchall():
                def _d(v):
                    if v is None:
                        return ""
                    return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v)
                results.append({
                    "cert": str(row[0] or "").strip().upper(),
                    "姓名": str(row[1] or ""),
                    "unit_code": int(row[2] or 0),
                    "unit_name": str(row[3] or ""),
                    "合同开始日期": _d(row[4]),
                    "合同终止日期": _d(row[5]),
                    "单位名称": str(row[6] or ""),
                    "经办人": str(row[7] or ""),
                })
    return results


def lookup_unit_codes_by_name(conn, unit_name: str) -> List[int]:
    """按结算单元名称 (TB93.ATB931) 查询结算单元代码 (TB93.ATB930)。

    返回全部匹配的代码 (名称可能对应多个代码), 无匹配返回空列表。
    """
    if not unit_name:
        return []
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT ATB930 FROM TB93 WHERE ATB931 = :n AND ATB930 IS NOT NULL",
            {"n": unit_name})
        return [int(r[0]) for r in cursor.fetchall()]
