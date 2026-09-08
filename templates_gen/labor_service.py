"""劳务报酬所得模板生成器 - 精确复制 demo TemplateFiller.cs 算法"""
from datetime import datetime
from typing import List, Optional
import os
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from models import SalaryRecord, GenerateResult
from templates_gen.explanation import add_explanation_sheet
from templates_gen.normal_salary import generate_tc93_full_sheet, generate_combo_list_sheet


def _remark_for(rec: SalaryRecord, title: str) -> str:
    """备注列取值: 结算单元名称(ATB931) → 结算单元代码 → 标题提取"""
    name = (rec.结算单元名称 or "").strip()
    if name:
        return name
    code = getattr(rec, "结算单元", 0) or 0
    if code:
        return str(code)
    return title


def generate_labor_service(records: List[SalaryRecord], title: str, output_dir: str,
                           tc93_all: Optional[List[dict]] = None,
                           combos: Optional[List[dict]] = None,
                           raw_records: Optional[List[SalaryRecord]] = None,
                           tc93_comments: Optional[dict] = None,
                           merge_mode: str = "pay_month",
                           pay_month: Optional[int] = None) -> GenerateResult:
    """生成劳务报酬所得 Excel 模板

    备注列固定填结算单元名称(ATB931), 与手工「07-月劳务报酬所得」文件一致。
    辅助 sheet (验证报告/TC93总表/报税结算单元/原始明细(未合并)/合并明细/验算公式说明)
    用于人工核查生成数据准确性, 与正常工资薪金模板结构对齐。
    """
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    output_path = os.path.join(output_dir, f"劳务报酬所得（不适用累计预扣法）_{timestamp}.xlsx")

    combo_map = {}
    if combos:
        for c in combos:
            key = (int(c.get("unit", 0) or 0), int(c.get("salary_month", 0) or 0), str(c.get("seq", "") or ""))
            combo_map[key] = f"{c.get('unit_name', '')}-{c.get('salary_month', '')}-{c.get('seq', '')}"

    wb = Workbook()
    ws = wb.active
    ws.title = "劳务报酬"

    # 14列标题 - 必须与 demo 完全一致
    headers = [
        "工号", "*姓名", "*证件类型", "*证件号码", "*所得项目", "*收入",
        "免税收入", "商业健康保险", "税延养老保险", "其他",
        "允许扣除的税费", "减免税额", "协定减免", "备注"
    ]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    # 写入数据
    for idx, rec in enumerate(records, 1):
        row = idx + 1
        ws.cell(row=row, column=1, value=rec.职工号)
        ws.cell(row=row, column=2, value=rec.姓名)
        ws.cell(row=row, column=3, value="居民身份证")
        ws.cell(row=row, column=4, value=rec.身份证)
        ws.cell(row=row, column=5, value="劳务报酬")
        ws.cell(row=row, column=6, value=rec.工资总额)
        ws.cell(row=row, column=14, value=_remark_for(rec, title))

    # 验证报告: 每行一人, 合并收入与原始明细合计自洽校验 (劳务报酬无左=右公式)
    vs = wb.create_sheet("验证报告")
    vs_headers = [
        "姓名", "证件号码", "结算单元名称", "所属月份", "批次", "经办人",
        "收入(合并工资总额)", "原始条数", "原始收入合计", "差值", "状态"
    ]
    for col, h in enumerate(vs_headers, 1):
        vs.cell(row=1, column=col, value=h)
    trip_handlers = {}
    for c in combos or []:
        key = (int(c.get("unit", 0) or 0), int(c.get("salary_month", 0) or 0),
               str(c.get("seq", "") or ""))
        h = str(c.get("handler", "") or "")
        if h and key not in trip_handlers:
            trip_handlers[key] = h
    tc93_handlers = {}
    for r in tc93_all or []:
        key = (int(r.get("ATB930", 0) or 0), int(r.get("ATC931", 0) or 0),
               str(r.get("ATC937", "") or ""))
        h = str(r.get("AAE019", "") or "")
        if h and key not in tc93_handlers:
            tc93_handlers[key] = h
    by_pay_month = merge_mode == "pay_month"
    raw_groups = {}
    if raw_records:
        for r in raw_records:
            key = (r.职工号,) if by_pay_month else (r.职工号, r.工资所属年月, r.当月批次)
            raw_groups.setdefault(key, []).append(r)
    pass_count = 0
    fail_count = 0
    for idx, rec in enumerate(records, 1):
        key = (rec.职工号,) if by_pay_month else (rec.职工号, rec.工资所属年月, rec.当月批次)
        raw_list = raw_groups.get(key, [])
        raw_total = sum((float(r.工资总额 or 0) for r in raw_list), 0.0) if raw_list else float(rec.工资总额 or 0)
        merged_income = float(rec.工资总额 or 0)
        diff = abs(merged_income - raw_total)
        passed = diff < 0.01
        if passed:
            pass_count += 1
        else:
            fail_count += 1
        handlers = []
        for r in raw_list:
            key = (int(r.结算单元 or 0), int(r.工资所属年月 or 0), str(r.当月批次 or ""))
            h = trip_handlers.get(key) or tc93_handlers.get(key) or ""
            if h and h not in handlers:
                handlers.append(h)
        if not handlers:
            key = (int(rec.结算单元 or 0), int(rec.工资所属年月 or 0), str(rec.当月批次 or ""))
            handlers = [trip_handlers.get(key) or tc93_handlers.get(key) or ""]
        vals = [
            rec.姓名, rec.身份证, _remark_for(rec, title),
            rec.工资所属年月, rec.当月批次, ";".join(h for h in handlers if h),
            merged_income, len(raw_list) if raw_list else 1, raw_total,
            round(diff, 4), "通过" if passed else "失败"
        ]
        for col, val in enumerate(vals, 1):
            vs.cell(row=idx + 1, column=col, value=val)
    for col, h in enumerate(vs_headers, 1):
        vs.column_dimensions[get_column_letter(col)].width = max(10, min(25, len(str(h)) * 1.5))

    # TC93总表 (全字段原始记录) + 报税结算单元 + 原始明细/合并明细 + 验算公式说明
    if tc93_all:
        generate_tc93_full_sheet(wb, tc93_all, tc93_comments)
    if combos:
        generate_combo_list_sheet(wb, combos)
    if raw_records:
        # TC8M批次人数含0工资挂账人员(不发钱), 原始明细仅保留本次报税人员记录, 否则与合并后条目严重失真
        final_gh = {r.职工号 for r in records}
        raw_report = [r for r in raw_records if r.职工号 in final_gh]
        generate_raw_detail_sheet(wb, raw_report, title, pay_month)
        generate_merge_detail_sheet(wb, raw_report, records, merge_mode)
    generate_formula_explanation_sheet(wb, records)

    add_explanation_sheet(wb, [
        ("劳务报酬", [
            "14 列个税劳务报酬申报模板，一行为一人。",
            "*所得项目 恒为「劳务报酬」；收入取工资总额（ATC93AA，原始值，不做扣减）。",
            "*证件类型 恒为「居民身份证」。",
            "备注 = 结算单元名称（TC93.ATB931），与手工「07-月劳务报酬所得」文件口径一致。",
        ]),
        ("验证报告", [
            "每行一人，验证 合并收入 = 原始明细收入合计（自洽校验，|差值|<0.01 为通过）。",
            "劳务报酬无左=右公式，本条校验保证按人合并过程可追溯。",
            "经办人 = 发放经办人(TC8M.AAE019)，取不到时回落做工资经办人(TC93.AAE019)；跨多批次分号连接。",
        ]),
        ("TC93总表", [
            "TC93 工资原始全字段，按身份证排序、同证相邻；重复次数=该身份证出现行数。",
        ]),
        ("报税结算单元", [
            "本次生成的结算单元组合（单元+所属月+发放月+批次+人数+合计收入+经办人）。",
        ]),
        ("原始明细(未合并)", [
            "合并前的逐条明细，便于追溯合并过程。",
        ]),
        ("合并明细", [
            "按人（+所属月或发放月）合并后的汇总与合并痕迹。",
        ]),
        ("验算公式说明", [
            "收入、合并、发放月份口径的详细说明。",
        ]),
    ])

    wb.save(output_path)

    return GenerateResult(
        file_path=output_path,
        template_type="劳务报酬所得（不适用累计预扣法）",
        record_count=len(records),
        validation_pass=pass_count,
        validation_fail=fail_count
    )


def generate_raw_detail_sheet(wb: Workbook, raw_records: List[SalaryRecord], title: str,
                              pay_month: Optional[int] = None):
    """原始明细(未合并)sheet：逐条列出未合并记录的报送数据，保证可追溯。"""
    ws = wb.create_sheet("原始明细(未合并)")
    headers = [
        "ATC930", "姓名", "证件号码", "结算单元", "结算单元名称", "所属月份", "批次", "发放月份",
        "收入(工资总额)", "个税", "实发", "备注"
    ]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)
    for idx, rec in enumerate(raw_records, 2):
        vals = [
            rec.tc930_id, rec.姓名, rec.身份证, rec.结算单元, rec.结算单元名称,
            rec.工资所属年月, rec.当月批次, pay_month,
            rec.工资总额, rec.个人所得税, rec.实发工资, _remark_for(rec, title)
        ]
        for col, val in enumerate(vals, 1):
            ws.cell(row=idx, column=col, value=val)
    for col, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(col)].width = max(10, min(25, len(str(h)) * 1.5))


def generate_merge_detail_sheet(wb: Workbook, raw_records: List[SalaryRecord],
                                merged_records: List[SalaryRecord],
                                merge_mode: str = "pay_month"):
    """合并明细sheet：按人分组，展示原始条目与合并后汇总，合并过程可追溯。

    merge_mode="month":     按人+所属月份分组
    merge_mode="pay_month": 按人分组，跨所属月份合并为一行
    """
    ws = wb.create_sheet("合并明细")
    by_pay_month = merge_mode == "pay_month"
    groups = {}
    for rec in raw_records:
        key = (rec.职工号,) if by_pay_month else (rec.职工号, rec.工资所属年月)
        groups.setdefault(key, []).append(rec)

    headers = [
        "姓名", "证件号码", "所属月份", "原始条数",
        "原始记录(ATC930/收入)", "合并后结算单元名称", "合并后批次",
        "合并收入", "合并个税", "合并实发"
    ]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    if by_pay_month:
        merged_map = {m.职工号: m for m in merged_records}
        ordered = sorted(groups.items(), key=lambda kv: (kv[0][0],))
    else:
        merged_map = {(m.职工号, m.工资所属年月): m for m in merged_records}
        ordered = sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    row = 2
    for key, recs in ordered:
        m = merged_map.get(key[0] if by_pay_month else key)
        if m is None:
            continue
        raw_desc = "; ".join(f"{r.tc930_id}/{float(r.工资总额 or 0)}" for r in recs)
        month_display = (";".join(str(x) for x in sorted({r.工资所属年月 for r in recs}))
                         if by_pay_month else key[1])
        vals = [
            recs[0].姓名, recs[0].身份证, month_display, len(recs),
            raw_desc, m.结算单元名称, m.当月批次,
            m.工资总额, m.个人所得税, m.实发工资
        ]
        for col, val in enumerate(vals, 1):
            ws.cell(row=row, column=col, value=val)
        row += 1


def generate_formula_explanation_sheet(wb: Workbook, records: List[SalaryRecord]):
    """验算公式说明 sheet：劳务报酬口径说明（无左=右校验）。"""
    ws = wb.create_sheet("验算公式说明")
    ws.column_dimensions["A"].width = 60
    ws.column_dimensions["B"].width = 25

    def w(row, col, text, bold=False):
        cell = ws.cell(row=row, column=col, value=text)
        if isinstance(text, str) and text.startswith("="):
            cell.data_type = "s"
        if bold:
            cell.font = cell.font.copy(bold=True)
        return cell

    w(1, 1, "劳务报酬所得（不适用累计预扣法）生成口径说明", bold=True)
    w(2, 1, "本模板适用于劳务报酬所得且不适用累计预扣法的申报场景，一行为一人，无左=右校验。")

    w(4, 1, "一、收入口径", bold=True)
    w(5, 1, "收入 = 本次工资总额(ATC93AA)，取原始值，不做任何扣减（免税/五险一金/个税均不参与）。")

    w(7, 1, "二、人员与发放月份口径", bold=True)
    w(8, 1, "发放月份 = TC8M.ATC8G7(经办年月)，且 ATC8M3='2'(已确认)。")
    w(9, 1, "同一发放月横跨多个所属月份(ATC931)，收入 = 各所属月 ATC93AA 之和（按人+发放月合并）。")
    w(10, 1, "仅保留 TC90.AAE00P='3'(劳务报酬) 且当前有效的合同人员（工资结束年月为空或 ≥ 发放月）。")
    w(11, 1, "仅保留工资总额>0 的人员（工资为0 不发钱，不进入申报模板）。")

    w(13, 1, "三、备注列", bold=True)
    w(14, 1, "备注 = 结算单元名称(TC93.ATB931)，与手工「07-月劳务报酬所得」文件一致。")

    w(16, 1, "四、辅助 sheet 验证逻辑", bold=True)
    w(17, 1, "验证报告：合并收入 与 原始明细收入合计 差值绝对值 <0.01 为通过（自洽校验）。")
    w(18, 1, "TC93总表：本次批次 TC93 原始全字段，人工可核对收入/结算单元与主表一致性。")
    w(19, 1, "报税结算单元：参与本次生成的结算单元组合列表。")
    w(20, 1, "原始明细(未合并) / 合并明细：合并前逐条记录与合并后汇总，追溯合并过程。")

    if records:
        w(22, 1, "五、示例（第一条记录）", bold=True)
        rec = records[0]
        ex_rows = [
            ("姓名", rec.姓名),
            ("证件号码", rec.身份证),
            ("结算单元名称", _remark_for(rec, "")),
            ("所属月份", rec.工资所属年月),
            ("批次", rec.当月批次),
            ("收入(工资总额)", rec.工资总额),
            ("个税", rec.个人所得税),
            ("实发", rec.实发工资),
        ]
        for i, (name, val) in enumerate(ex_rows, 23):
            w(i, 1, name)
            w(i, 2, val)