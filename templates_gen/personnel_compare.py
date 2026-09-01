"""增减员比对 - 51列模板列头定义与数据映射

将个税端导出人员信息与当月发薪人员比对后, 增员/减员名单均以
人员信息采集导入模板的 51 列格式输出 (与 personnel_info.py 的
列头完全一致)。

列号索引 (0-based, 对应 51 列):
- 0: 工号, 1: *姓名, 2: *证件类型, 3: *证件号码, 4: *国籍(地区), 5: *性别
- 6: *出生日期, 7: 是否高级专家, 8: *任职受雇从业类型, 9: 其他情况说明
- 10: 入职年度就业情形, 11: 手机号码, 12: 任职受雇从业日期, 13: 离职日期
- 14: 是否离职后补发工资, ..., 25: 备注, ..., 50: 职务
"""
from datetime import datetime
from typing import List

from openpyxl import Workbook

from models import GenerateResult, PersonnelInfo

# 51 列标题 - 与 personnel_info.py 完全一致
COMPARE_HEADERS: List[str] = [
    "工号", "*姓名", "*证件类型", "*证件号码", "*国籍(地区)", "*性别",
    "*出生日期", "是否高级专家", "*任职受雇从业类型", "其他情况说明",
    "入职年度就业情形", "手机号码", "任职受雇从业日期", "离职日期",
    "是否离职后补发工资", "实际补发工资的月份", "是否残疾", "是否烈属",
    "是否孤老", "残疾证件类型", "残疾证号", "烈属证号", "是否扣除减除费用",
    "个人投资额", "个人投资比例(%)", "备注", "中文名", "涉税事由",
    "出生国家(地区)", "首次入境时间", "预计离境时间", "其他证件类型",
    "其他证件号码", "户籍所在地（省）", "户籍所在地（市）", "户籍所在地（区县）",
    "户籍所在地（详细地址）", "经常居住地（省）", "经常居住地（市）",
    "经常居住地（区县）", "经常居住地（详细地址）", "联系地址（省）",
    "联系地址（市）", "联系地址（区县）", "联系地址（详细地址）",
    "电子邮箱", "学历", "开户银行", "银行账号", "开户行省份", "职务"
]

# 常用字段的列索引 (0-based)
IDX_工号 = 0
IDX_姓名 = 1
IDX_证件类型 = 2
IDX_证件号码 = 3
IDX_国籍 = 4
IDX_性别 = 5
IDX_出生日期 = 6
IDX_任职受雇从业类型 = 8
IDX_手机号码 = 11
IDX_任职受雇从业日期 = 12
IDX_离职日期 = 13


def _normalize_date(value) -> str:
    """将日期值规范化为 "YYYY-MM-DD" 字符串。

    支持 datetime 对象、xlrd 日期序列号 (float)、或已有字符串。
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and not value.is_integer():
        try:
            import xlrd
            return xlrd.xldate_as_datetime(value, 0).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return str(value)
    s = str(value).strip()
    # 兼容 "1981-10-26 00:00:00" 截断为日期
    if len(s) > 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return s


def map_personnel_info_to_row(person: PersonnelInfo) -> list:
    """将数据库人员信息 (增员) 映射为 51 列行数据。"""
    row = [""] * len(COMPARE_HEADERS)
    row[IDX_工号] = person.职工号
    row[IDX_姓名] = person.姓名
    row[IDX_证件类型] = person.证件类型 or "居民身份证"
    row[IDX_证件号码] = person.身份证
    row[IDX_国籍] = person.国籍 or "中国"
    row[IDX_性别] = person.性别
    row[IDX_出生日期] = _normalize_date(person.出生日期)
    row[IDX_任职受雇从业类型] = person.任职类型 or "雇员"
    row[IDX_手机号码] = person.手机号码
    row[IDX_任职受雇从业日期] = _normalize_date(person.任职受雇从业日期)
    return row


def map_tax_export_to_row(person: dict, termination_date=None) -> list:
    """将个税端导出记录 (减员) 映射为 51 列行数据。

    termination_date 为 TC90.ATC90D 合同终止日期 (datetime 或 str),
    填入"离职日期"列标记减员。
    """
    row = [""] * len(COMPARE_HEADERS)
    row[IDX_工号] = str(person.get("工号") or "")
    row[IDX_姓名] = str(person.get("姓名") or "")
    row[IDX_证件类型] = str(person.get("证件类型") or "") or "居民身份证"
    row[IDX_证件号码] = str(person.get("证件号码") or "")
    row[IDX_国籍] = str(person.get("国籍") or "") or "中国"
    row[IDX_性别] = str(person.get("性别") or "")
    row[IDX_出生日期] = _normalize_date(person.get("出生日期"))
    row[IDX_任职受雇从业类型] = str(person.get("任职受雇从业类型") or "") or "雇员"
    row[IDX_手机号码] = str(person.get("手机号码") or "")
    row[IDX_任职受雇从业日期] = _normalize_date(person.get("任职受雇从业日期"))
    if termination_date:
        row[IDX_离职日期] = _normalize_date(termination_date)
    return row


def _cert_key(value) -> str:
    return str(value or "").strip().upper()


def compare_personnel(tax_export_persons, payroll_certs, payroll_personnel, termination_dates,
                      unpaid_persons=None, contract_signed_persons=None,
                      person_units=None, filter_handlers=None, filter_units=None,
                      exclude_certs=None):
    """增减员比对引擎。

    Args:
        tax_export_persons: List[dict] - 个税端导出文件解析结果
        payroll_certs: Set[str] - 发薪月份范围内发薪人员证件号集合 (已大写, 并集)
        payroll_personnel: List[PersonnelInfo] - 发薪人员详细信息
        termination_dates: Dict[str, datetime] - 证件号 → TC90 合同终止日期
        unpaid_persons: Set[str] - 未发薪工资表人员证件号集合 (减员排除 + 增员条件②)
        contract_signed_persons: Set[str] - 合同签署时间范围内人员 (增员条件③)
        person_units: Dict[str, dict] - 证件号 → {handler, unit_code, unit_name}
        filter_handlers: List[str] - 经办人过滤 (空=不过滤)
        filter_units: List[int] - 结算单元代码过滤 (空=不过滤)
        exclude_certs: Set[str] - 需从增员中排除的人员 (特殊结算单元工资为0)

    Returns:
        (add_rows, departed_rows, pending_rows, stats):
            add_rows: List[list] - 增员 51 列行数据
            departed_rows: List[list] - 近期离职人员 51 列行数据
            pending_rows: List[list] - 待确认近期离职人员 51 列行数据
            stats: dict - {add_count, departed_count, pending_count, tax_total, payroll_total}
    """
    unpaid_set = {_cert_key(c) for c in (unpaid_persons or set())}
    unpaid_set.discard("")
    contract_set = {_cert_key(c) for c in (contract_signed_persons or set())}
    contract_set.discard("")
    exclude_set = {_cert_key(c) for c in (exclude_certs or set())}
    exclude_set.discard("")

    handlers = {h for h in (filter_handlers or []) if h}
    units = {int(u) for u in (filter_units or []) if u}
    person_units = person_units or {}

    def _passes_filter(cert):
        if not handlers and not units:
            return True
        info = person_units.get(_cert_key(cert))
        if not info:
            return False
        if handlers and info.get("handler") not in handlers:
            return False
        if units and info.get("unit_code") not in units:
            return False
        return True

    tax_certs = {_cert_key(p.get("证件号码")) for p in tax_export_persons}
    tax_certs.discard("")

    payroll_set = {_cert_key(c) for c in payroll_certs}
    payroll_set.discard("")

    # 增员 = (发薪 ∪ 未发薪工资表 ∪ 合同签署) - 个税端, 再按经办人/结算单元过滤
    add_certs = (payroll_set | unpaid_set | contract_set) - tax_certs
    add_certs = {c for c in add_certs if _passes_filter(c)}
    add_certs -= exclude_set

    # 疑似离职: 个税端未标记离职(无离职日期)且不在发薪/未发薪名单
    # (个税端已有离职日期的属历史离职, 不参与近期离职判定)
    active_certs = {_cert_key(p.get("证件号码")) for p in tax_export_persons
                    if not str(p.get("离职日期") or "").strip()}
    protected_certs = payroll_set | unpaid_set | contract_set
    suspect_certs = active_certs - protected_certs
    suspect_certs = {c for c in suspect_certs if _passes_filter(c)}
    departed_certs = {c for c in suspect_certs if c in termination_dates}
    pending_certs = suspect_certs - departed_certs

    add_rows = []
    for person in payroll_personnel:
        if _cert_key(person.身份证) in add_certs:
            add_rows.append(map_personnel_info_to_row(person))

    departed_rows = []
    for person in tax_export_persons:
        cert = _cert_key(person.get("证件号码"))
        if cert in departed_certs:
            departed_rows.append(map_tax_export_to_row(
                person, termination_date=termination_dates.get(cert)))

    pending_rows = []
    for person in tax_export_persons:
        cert = _cert_key(person.get("证件号码"))
        if cert in pending_certs:
            pending_rows.append(map_tax_export_to_row(person))

    stats = {
        "add_count": len(add_certs),
        "departed_count": len(departed_certs),
        "pending_count": len(pending_certs),
        "tax_total": len(tax_certs),
        "payroll_total": len(payroll_set),
    }
    return add_rows, departed_rows, pending_rows, stats


def generate_compare_excel(add_rows, departed_rows, pending_rows, stats, output_dir: str, pay_month: int) -> GenerateResult:
    """生成增减员比对结果 Excel (三 Sheet: 增员名单 + 近期离职人员 + 待确认近期离职人员)。"""
    import os
    from datetime import datetime as _dt

    os.makedirs(output_dir, exist_ok=True)
    timestamp = _dt.now().strftime("%Y%m%d%H%M%S")
    output_path = os.path.join(output_dir, f"增减员比对结果_{pay_month}_{timestamp}.xlsx")

    wb = Workbook()
    ws_add = wb.active
    ws_add.title = "增员名单"
    ws_add.append(COMPARE_HEADERS)
    for row in add_rows:
        ws_add.append(row)

    ws_departed = wb.create_sheet("近期离职人员")
    ws_departed.append(COMPARE_HEADERS)
    for row in departed_rows:
        ws_departed.append(row)

    ws_pending = wb.create_sheet("待确认近期离职人员")
    ws_pending.append(COMPARE_HEADERS)
    for row in pending_rows:
        ws_pending.append(row)

    wb.save(output_path)
    return GenerateResult(
        file_path=output_path,
        template_type="增减员比对结果",
        record_count=len(add_rows) + len(departed_rows) + len(pending_rows),
        validation_pass=0,
        validation_fail=0,
    )