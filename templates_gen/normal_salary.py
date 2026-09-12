"""正常工资薪金所得模板生成器 - 精确复制 demo TemplateFiller.cs 算法"""
from datetime import datetime
from typing import List, Optional
from collections import Counter
import os
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from models import SalaryRecord, GenerateResult
from templates_gen.formulas import calc_本期收入
from templates_gen.explanation import add_explanation_sheet


def extract_remark(title: str) -> str:
    """从标题提取备注字段"""
    if not title:
        return ""
    t = title.replace("东北师范大学人事处", "").replace("劳务派遣人员工资发放表", "").strip()
    t = t.replace("年", "").replace("月", "").replace("系统", "").strip()
    digits = ''.join(c for c in t if c.isdigit())
    if len(digits) >= 4:
        t = t.replace(digits, "").strip()
    return t if t else title


def build_remark_text(combos: Optional[List[dict]]) -> str:
    """生成收入表备注：只写该人员的一个结算单元名称（combos 已按主结算单元/首薪单元优先排序，取第一个）。

    规则：
    - combos[0] = 主结算单元（若无主单元则当月第一次发薪的结算单元）；
    - 仅列结算单元名称（不含所属年月-批次，与验证报告"结算单元名称-所属月份-批次"列避免重复）；
    - 超 50 字符截断。
    """
    if not combos:
        return ""
    c = combos[0]
    name = str(c.get("unit_name") or c.get("unit") or "").strip()
    return name if len(name) <= 50 else name[:50]


def _trip_seq_num(seq) -> int:
    try:
        return int(seq or 0)
    except (ValueError, TypeError):
        return 0


def _first_pay_unit(trips) -> int:
    """该人员最早发薪的结算单元：最早所属月 → 最小批次 → 最小单元代码。"""
    return min(trips, key=lambda t: (t[1], _trip_seq_num(t[2]), t[0]))[0]


def _main_unit_for(trips, main_units) -> int:
    """判定该人员归属的主结算单元（与 tax_merge._main_unit_of 同规则）：

    人员发薪所涉单元 ∩ 主单元集合，取其中最早发薪（最早所属月，平局最小代码）；
    无交集 → 回落当月第一次发薪的结算单元。
    """
    units = {t[0] for t in trips}
    pool = units & set(main_units or set())
    if pool:
        first_month = {u: min(m for (u2, m, _s) in trips if u2 == u) for u in pool}
        return min(pool, key=lambda u: (first_month[u], u))
    return _first_pay_unit(trips)


def generate_normal_salary(records: List[SalaryRecord], title: str, output_dir: str,
                           tc93_all: Optional[List[dict]] = None,
                           abnormal: Optional[List[dict]] = None,
                           abnormal_reasons: Optional[dict] = None,
                           combos: Optional[List[dict]] = None,
                           tc93_comments: Optional[dict] = None,
                           raw_records: Optional[List[SalaryRecord]] = None,
                           merge_mode: str = "month",
                           main_units: Optional[set] = None,
                           annual_avg_wage: float = 120000,
                           merge_choices: Optional[dict] = None,
                           zero_choices: Optional[dict] = None,
                           persist_merge_choices: bool = False,
                           persist_zero_choices: bool = False) -> GenerateResult:
    """生成正常工资薪金所得 Excel 模板

    新增 tc93_all: TC93总表(全字段), abnormal: 异常记录, abnormal_reasons: 过滤原因
    merge_mode: "month"=按人+所属月份合并, "pay_month"=按人+发放月份合并(每人一行)
    annual_avg_wage: 年平均工资总额(默认12万)，验证报告按 3 倍判断经济补偿是否达交税标准
    merge_choices: {证件号: 'double'|'single'|'skip'} 用户确认的三险一金合并方式，写入"合并验证"sheet
    zero_choices: {证件号: 'declare'|'skip'} 用户确认的零申报选择，写入"零申报验证"sheet
    persist_merge_choices/persist_zero_choices: 本次是否勾选"记住"（持久化）, 验证 sheet 记录
    """
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    output_path = os.path.join(output_dir, f"正常工资薪金所得_{title}_{timestamp}.xlsx")

    # 单元组合映射: (unit, month, seq) -> 中文名 / 名-年月-批次
    combo_map = {}        # -> "名称-所属年月-批次"（供合并明细等追溯 sheet）
    unit_name_map = {}    # -> 纯结算单元名称（备注/结算单元名称列用）
    for c in combos or []:
        key = (int(c.get("unit", 0) or 0), int(c.get("salary_month", 0) or 0),
               str(c.get("seq", "") or ""))
        uname = str(c.get("unit_name", "") or "")
        combo_map[key] = f"{uname}-{c.get('salary_month', '')}-{c.get('seq', '')}"
        unit_name_map[key] = uname

    # 人员排序：按结算单元名称排序，相同结算单元相邻（同单元内按姓名、工号稳定）
    def _unit_sort(r):
        try:
            emp_no = int(r.职工号 or 0)
        except (ValueError, TypeError):
            emp_no = 0
        return (str(getattr(r, "结算单元名称", "") or ""), str(r.姓名 or ""), emp_no)

    records = sorted(records, key=_unit_sort)
    if raw_records:
        raw_records = sorted(raw_records, key=lambda r: _unit_sort(r) + (int(r.tc930_id or 0),))

    # 每人实际涉及的组合（合并前 raw_records 按人分组；未合并时逐条独立）
    by_pay_month = merge_mode == "pay_month"

    def _person_key(r):
        return (r.职工号,) if by_pay_month else (r.职工号, r.工资所属年月)

    if raw_records:
        combo_src = raw_records
        key_fn = _person_key
    else:
        combo_src = records
        key_fn = lambda r: (int(r.tc930_id or 0),)
    person_trips = {}  # key -> {(unit, month, seq)}
    for r in combo_src:
        person_trips.setdefault(key_fn(r), set()).add(
            (int(r.结算单元 or 0), int(r.工资所属年月 or 0), str(r.当月批次 or "")))

    def _row_combos(rec):
        """该行人员实际涉及的组合（按主结算单元/首薪单元优先排序），备注/组合列数据源。

        顺序: 主结算单元(若有) → 当月第一次发薪单元(若不同) → 其余按最早发薪月、单元名称。
        """
        trips = person_trips.get(_person_key(rec)) or {(int(rec.结算单元 or 0),
                                                        int(rec.工资所属年月 or 0),
                                                        str(rec.当月批次 or "")),
                                                       }
        default_name = str(getattr(rec, "结算单元名称", "") or "")
        main_unit = _main_unit_for(trips, main_units)
        first_unit = _first_pay_unit(trips)
        first_month = {u: min(m for (_u, m, _s) in trips if _u == u)
                       for u in {t[0] for t in trips}}
        name_of = {u: (unit_name_map.get(next(
            t for t in trips if t[0] == u), "") or default_name) for u in {t[0] for t in trips}}

        def _sort_key(t):
            u = t[0]
            rank = 0 if u == main_unit else 1 if u == first_unit else 2
            return (rank, first_month[u], name_of[u], u)

        ordered = sorted(trips, key=_sort_key)
        combos = [{"unit": t[0], "unit_name": name_of[t[0]],
                   "salary_month": t[1], "seq": t[2]} for t in ordered]
        combo_full = ";".join(f"{name_of[t[0]]}-{t[1]}-{t[2]}" for t in ordered)
        return combos, (combo_full or title)
    
    wb = Workbook()
    ws = wb.active
    ws.title = "正常工资薪金收入"
    
    # 30列标题 - 与新版 liuxc 模板一致（住房公积金调整为占位列，不填数据）
    headers = [
        "工号", "*姓名", "*证件类型", "*证件号码", "本期收入", "本期免税收入",
        "基本养老保险费", "基本医疗保险费", "失业保险费", "住房公积金",
        "累计子女教育", "累计继续教育", "累计住房贷款利息", "累计住房租金",
        "累计赡养老人", "累计3岁以下婴幼儿照护", "累计个人养老金", "企业(职业)年金",
        "商业健康保险", "税延养老保险", "公务交通费用", "通讯费用", "律师办案费用",
        "住房公积金调整", "西藏附加减除费用", "其他", "准予扣除的捐赠额", "减免税额", "协定减免", "备注"
    ]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)
    # 工号列留空：人员标识实际取用的职工号(ID)单独列于第31列"工号(实际取用ID)"
    ws.cell(row=1, column=len(headers) + 1, value="工号(实际取用ID)")
    validations = []
    income_rows = []  # 收入表每行30列值, 供验证报告逐行1:1复制
    for idx, rec in enumerate(records, 1):
        row = idx + 1
        income = calc_本期收入(rec)
        own_combos, own_combo_full = _row_combos(rec)
        remark_text = build_remark_text(own_combos) or title

        ws.cell(row=row, column=2, value=rec.姓名)
        ws.cell(row=row, column=3, value="居民身份证")
        ws.cell(row=row, column=4, value=rec.身份证)
        ws.cell(row=row, column=5, value=income)
        ws.cell(row=row, column=7, value=rec.养老个人)
        ws.cell(row=row, column=8, value=rec.医疗个人)
        ws.cell(row=row, column=9, value=rec.失业个人)
        ws.cell(row=row, column=10, value=rec.公积金个人)
        ws.cell(row=row, column=18, value=0)  # 企业(职业)年金 = 0
        ws.cell(row=row, column=30, value=remark_text)
        ws.cell(row=row, column=31, value=rec.职工号)
        income_rows.append([
            None, rec.姓名, "居民身份证", rec.身份证, income, None,
            rec.养老个人, rec.医疗个人, rec.失业个人, rec.公积金个人,
            None, None, None, None, None, None, None, 0,
            None, None, None, None, None, None, None, None, None, None, None, remark_text,
            rec.职工号,
        ])
        
        # 左 = 本期收入 − 养老 − 失业 − 医疗 − 公积金 − 意外险 + 本次免税(ATC936)
        # 本期收入内部已减本次免税与大病险个人(ATC93BD)，此处加回本次免税使左式回到工资总额口径；
        # 大病险个人左右两侧同项（收入已减、实发已刨除），销项不再单列（见 docs/本期收入算法说明.md）
        left = income - rec.养老个人 - rec.失业个人 - rec.医疗个人 - rec.公积金个人 \
               - rec.意外险个人 + rec.补发3
        # 右 = (实发 − 经济补偿金) + 工会会费 + 代理费 + 个税 + 个人其他调整(ATC93AG)
        # 实发金额已代扣工会会费、个税、个人其他调整，需加回复原；经济补偿金(ATC93M)含在实发中
        # 但属一次性补偿，不参与正常工资薪金验算，故从实发扣回
        right = (rec.实发工资 - rec.经济补偿金) + rec.税后工会会费 + rec.个人代理费 + rec.个人所得税 + rec.个人其他调整
        diff = abs(left - right)
        passed = diff < 0.01
        comp_tax_std = "不涉及" if not rec.经济补偿金 else (
            "已达到" if rec.经济补偿金 > 3 * annual_avg_wage else "未达到")
        validations.append({
            "tc930": rec.tc930_id, "姓名": rec.姓名,
            "unit": rec.结算单元,
            # 结算单元名称列: 全部发薪的结算单元名称, 主结算单元/首薪单元优先排序
            "unit_name": ",".join(dict.fromkeys(
                str(c.get("unit_name") or "").strip() for c in own_combos)),
            "combo_full": own_combo_full,
            "salary_month": rec.工资所属年月, "seq": rec.当月批次,
            "工资总额": rec.工资总额, "本次免税": rec.补发3, "大病险个人": rec.大病险个人,
            "补缴退款差额": rec.补缴及退款保险金额个人, "交纳现金": rec.个人交纳现金, "本期收入": income,
            "养老": rec.养老个人, "失业": rec.失业个人, "医疗": rec.医疗个人, "公积金": rec.公积金个人,
            "其他调整": rec.个人其他调整, "个人欠款": rec.个人欠款, "意外险": rec.意外险个人,
            "左": left,
            "实发": rec.实发工资, "工会会费": rec.税后工会会费, "代理费": rec.个人代理费,
            "个税": rec.个人所得税,
            "经济补偿金": rec.经济补偿金, "达交税标准": comp_tax_std,
            "右": right, "差值": diff, "通过": passed
        })
    
    # 验证报告与收入表逐行一一对应: 收入表30列原样复制 + 新增组合合并列/发放经办人 + 原验算列右移
    vs = wb.create_sheet("验证报告")
    thr = 3 * annual_avg_wage
    vs_headers = headers + ["工号(实际取用ID)"] + [
        "结算单元名称-所属月份-批次", "发放经办人",
        "ATC930", "姓名", "结算单元名称", "所属月份", "批次",
        "本次工资总额(ATC93AA)", "本次免税(ATC936)", "大病险（个人承担）(ATC93BD)", "补缴及退款保险差额（个人）(ATC93BE)", "个人交纳现金(ATC93X3)", "本期收入",
        "当月养老个人缴(BAA001)", "当月失业个人缴(BAA003)", "当月医疗个人缴(BAA002)", "个人公积金月缴存额(CAA002)",
        "意外险个人(ATC93BH)", "个人欠款(ATC93E)", "左",
        "本次实发金额(ATC93C)", "税后扣除工会会费(ATC93Z2)", "个人承担代理费(BAA300)", "本次个人所得税(ATC93D)", "个人其他调整(ATC93AG)",
        "经济补偿金(ATC93M)", f"经济补偿是否达交税标准(基准=3×年平均工资{thr:,.0f})",
        "右", "差值", "状态"
    ]
    for col, h in enumerate(vs_headers, 1):
        vs.cell(row=1, column=col, value=h)
    # 经办人: 发放经办人(TC8M.AAE019, 来自combos.handler)优先, 回落做工资经办人(TC93.AAE019)
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
    for idx, v in enumerate(validations, 1):
        key = (int(v["unit"] or 0), int(v["salary_month"] or 0), str(v["seq"] or ""))
        handler = trip_handlers.get(key) or tc93_handlers.get(key) or ""
        vals = list(income_rows[idx - 1]) + [v["combo_full"], handler] + [
            v["tc930"], v["姓名"], v["unit_name"], v["salary_month"], v["seq"],
            v["工资总额"], v["本次免税"], v["大病险个人"], v["补缴退款差额"], v["交纳现金"], v["本期收入"],
            v["养老"], v["失业"], v["医疗"], v["公积金"],
            v["意外险"], v["个人欠款"], v["左"],
            v["实发"], v["工会会费"], v["代理费"], v["个税"], v["其他调整"],
            v["经济补偿金"], v["达交税标准"],
            v["右"], v["差值"], "通过" if v["通过"] else "失败"
        ]
        for col, val in enumerate(vals, 1):
            if val is not None:
                vs.cell(row=idx+1, column=col, value=val)
    
    if tc93_all:
        generate_tc93_full_sheet(wb, tc93_all, tc93_comments)
    if abnormal:
        generate_abnormal_sheet(wb, abnormal, abnormal_reasons or {})
    if combos:
        generate_combo_list_sheet(wb, combos)
    if raw_records:
        generate_raw_detail_sheet(wb, raw_records, combo_map, title)
        generate_merge_detail_sheet(wb, raw_records, records, merge_mode)
    generate_formula_explanation_sheet(wb, records)
    generate_field_mapping_sheet(wb)
    if merge_choices or zero_choices:
        name_map = _build_person_name_map(records, raw_records, tc93_all, abnormal)
    if merge_choices:
        generate_merge_verification_sheet(wb, merge_choices, name_map, persist_merge_choices)
    if zero_choices:
        generate_zero_verification_sheet(wb, zero_choices, records, name_map, persist_zero_choices)
    add_explanation_sheet(wb, [
        ("正常工资薪金收入", [
            "30 列个税申报模板（含占位列'住房公积金调整'），一行为一人（按人合并）。",
            "本期收入 = 应发工资 − （独生子女费+采暖费） − 大病险（个人） − 补缴及退款保险差额（个人） + 交纳现金 − 个人欠款。",
            "字段与算法详见 docs/本期收入算法说明.md；ATC 字段注释见 docs/数据表字段注释.md。",
            "五险一金列取个人缴部分，企业(职业)年金恒为 0，备注填该人员归属的结算单元名称（有主结算单元写主单元，否则写当月第一次发薪的结算单元，仅一个名称≤50字符）。",
            "工号列按需求留空不填；系统实际取用的人员标识（职工号ID）单独列于第31列'工号(实际取用ID)'：",
            "正常人员=AC01.AAC001 个人编号；名单零申报注入人员（无个人编号、职工号兜底为证件号）则该列=证件号。",
        ]),
        ("验证报告", [
            "与'正常工资薪金收入'sheet 逐行一一对应：前30列为收入表原样复制（工号列同样留空），",
            "第31列'工号(实际取用ID)'与收入表一致，随后为该行人员实际涉及的组合列（结算单元-所属月-批次，不受字数限制，该人员跨多个组合分号连接）与发放经办人，再向右为原验算列。",
            "左=右校验：左 = 本期收入 − 养老 − 失业 − 医疗 − 公积金 − 意外险 + 本次免税(ATC936)；",
            "右 = (实发 − 经济补偿金) + 税后工会会费 + 个人代理费 + 个税 + 个人其他调整(ATC93AG)；|左−右|<0.01 为通过。",
            "大病险个人(ATC93BD)左右两侧同项销项不单列；经济补偿金(ATC93M)含在实发中但属一次性补偿，",
            "验证时从实发扣回；另按 3×年平均工资判断是否达交税标准。公式推导详见'验算公式说明'sheet。",
        ]),
        ("合并验证", [
            "记录本次生成时用户逐人确认的三险一金合并方式（'检查合并规则并确认'弹窗的选择）：",
            "多月合并(double)：三险一金跨所属月全部合计（默认口径，未在主账弹窗确认的人员按此处理）；",
            "单月(single)：三险一金只取最近一次三险>0的所属月（发放月有三险则=发放月）；",
            "不报(skip)：三险一金全按0上报（收入/个税仍正常合计；仅发放月无三险时生效，发放月有三险时自动回落多月合并）。",
            "'本次已记住'=勾选记住时写入 SQLite merge_override 表，下次确认弹窗默认选中该项。",
        ]),
        ("零申报验证", [
            "记录本次生成时用户对本期收入为0人员是否生成零申报的逐人确认（'检查零申报并确认'弹窗的选择）：",
            "生成(declare)：保留/注入该人员零申报记录（收入与五险一金全0上报）；",
            "不生成(skip)：该人员零申报被剔除，不出现在本文件收入表中。",
            "未在零申报弹窗确认的零收入人员默认保留（生成零申报）；'本次已记住'=写入 SQLite zero_override 表，下次弹窗默认选中。",
        ]),
        ("TC93总表", [
            "TC93 工资原始全字段，按身份证排序、同证相邻；重复次数=该身份证出现行数。",
            "无需用户确认，生成时自动从 TC93 查询原始工资记录（只读 SELECT）。",
        ]),
        ("异常记录(已过滤)", [
            "状态异常被过滤的条目，附过滤原因；无需用户确认，自动过滤。",
        ]),
        ("报税结算单元", [
            "本次生成的结算单元组合（单元+所属月+发放月+批次+人数+合计收入+经办人）。",
            "由所选批次自动聚合生成，无需用户确认。",
        ]),
        ("原始明细(未合并)", [
            "合并前的逐条明细，便于追溯合并过程；自动生成，无需用户确认。",
        ]),
        ("合并明细", [
            "按人（+所属月或发放月）合并后的汇总与合并痕迹；自动生成，无需用户确认。",
            "用户对合并方式的逐人确认选择记录在'合并验证'sheet。",
        ]),
        ("验算公式说明", [
            "本期收入、左、右公式的详细推导、字段对照与实际示例。",
        ]),
        ("字段对应关系", [
            "六列：序号 | 区块 | 工资表薪资明细名称 | 数据库字段名称 | 数据库字段注释 | 备注。",
            "按实际 57 列工资表逐列映射，字段注释严格采用数据库 ALL_COL_COMMENTS 原注释；",
            "TC8M/TC93 批次级字段一并列出，工资表无对应列的名称留空。",
        ]),
    ])
    
    wb.save(output_path)
    
    pass_count = sum(1 for v in validations if v["通过"])
    fail_count = len(validations) - pass_count
    
    return GenerateResult(
        file_path=output_path,
        template_type="正常工资薪金所得",
        record_count=len(records),
        validation_pass=pass_count,
        validation_fail=fail_count
    )


def generate_tc93_full_sheet(wb: Workbook, tc93_all: List[dict], comments: Optional[dict] = None):
    """TC93总表 sheet：身份证/重复次数/经办年月置最左，按身份证排序，同身份证相邻。"""
    if not tc93_all:
        return
    ws = wb.create_sheet("TC93总表")
    tc93_all = sorted(tc93_all, key=lambda r: (not r.get("身份证"), str(r.get("身份证") or ""),
                                               int(r.get("ATC930") or 0)))
    cols = list(tc93_all[0].keys())
    first_cols = [c for c in ("身份证", "ATC8G7") if c in cols]
    cols = first_cols + [c for c in cols if c not in first_cols]
    id_counts = Counter(rec.get("身份证", "") for rec in tc93_all)
    if "身份证" in cols:
        cols.insert(1, "重复次数")
    for col_idx, col_name in enumerate(cols, 1):
        if col_name == "重复次数":
            ws.cell(row=1, column=col_idx, value="该身份证号在本sheet中出现的行数")
        else:
            comment = (comments or {}).get(col_name, "")
            ws.cell(row=1, column=col_idx, value=comment if comment else col_name)
        ws.cell(row=2, column=col_idx, value=col_name)
    for row_idx, rec in enumerate(tc93_all, 3):
        for col_idx, col_name in enumerate(cols, 1):
            if col_name == "重复次数":
                val = id_counts.get(rec.get("身份证", ""), 0)
            else:
                val = rec.get(col_name)
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            if isinstance(val, str) and val.startswith("="):
                cell.data_type = "s"
    for col_idx, col_name in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = max(10, min(25, len(str(col_name)) * 1.5))


def generate_abnormal_sheet(wb: Workbook, abnormal: List[dict], reasons: dict):
    """异常记录sheet，列出被状态过滤的条目及原因。"""
    if not abnormal:
        return
    ws = wb.create_sheet("异常记录(已过滤)")
    base_cols = ["AAC001", "AAC003", "ATC931", "ATC937", "ATC930", "ATB930", "ATC93AA", "ATC93C", "ATC93D", "ATC93G", "ATC93N", "ATC93U", "ATC93V", "ATC93W", "ATC93AE"]
    headers = ["职工号", "姓名", "所属年月", "批次", "流水号", "结算单元", "工资总额", "实发金额", "个税", "结算状态", "上月工资", "可发情况", "费用状态", "个人欠费", "偿还", "过滤原因"]
    for col_idx, h in enumerate(headers, 1):
        ws.cell(row=1, column=col_idx, value=h)
    for row_idx, rec in enumerate(abnormal, 2):
        tc930 = rec.get("ATC930")
        for col_idx, col_name in enumerate(base_cols, 1):
            ws.cell(row=row_idx, column=col_idx, value=rec.get(col_name))
        ws.cell(row=row_idx, column=len(base_cols) + 1, value=reasons.get(tc930, "状态异常"))
    # 自动列宽
    for col_idx, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = max(10, min(25, len(str(h)) * 1.5))


def generate_combo_list_sheet(wb: Workbook, combos: List[dict]):
    """待报列表 sheet，列出本次生成的结算单元组合。"""
    if not combos:
        return
    ws = wb.create_sheet("报税结算单元")
    headers = ["结算单元ID", "结算单元名称", "所属月份", "发放月份", "批次", "人数", "合计收入", "经办人"]
    for col_idx, h in enumerate(headers, 1):
        ws.cell(row=1, column=col_idx, value=h)
    for row_idx, c in enumerate(combos, 2):
        ws.cell(row=row_idx, column=1, value=c.get("unit", ""))
        ws.cell(row=row_idx, column=2, value=c.get("unit_name", ""))
        ws.cell(row=row_idx, column=3, value=c.get("salary_month", ""))
        ws.cell(row=row_idx, column=4, value=c.get("pay_month", ""))
        ws.cell(row=row_idx, column=5, value=c.get("seq", ""))
        ws.cell(row=row_idx, column=6, value=c.get("person_count", ""))
        ws.cell(row=row_idx, column=7, value=c.get("total_income", ""))
        ws.cell(row=row_idx, column=8, value=c.get("handler", ""))
    for col_idx, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = max(10, min(25, len(str(h)) * 1.5))


def generate_formula_explanation_sheet(wb: Workbook, records: List[SalaryRecord]):
    """验算公式说明 sheet，逐项解释左=右校验。"""
    ws = wb.create_sheet("验算公式说明")
    ws.column_dimensions["A"].width = 55
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 55

    def w(row, col, text, bold=False):
        cell = ws.cell(row=row, column=col, value=text)
        if isinstance(text, str) and text.startswith("="):
            cell.data_type = "s"
        if bold:
            cell.font = cell.font.copy(bold=True)
        return cell

    w(1, 1, "验证报告验算公式说明", bold=True)
    w(2, 1, "验证报告 sheet 中每一行对一个人的工资数据进行左=右校验：左与右的差值绝对值小于 0.01 即为通过。")

    w(4, 1, "一、本期收入（报税口径）", bold=True)
    w(5, 1, "本期收入 = 本次工资总额(ATC93AA) − 本次免税(ATC936) − 大病险个人(ATC93BD) − 补缴及退款保险差额个人(ATC93BE) + 个人交纳现金(ATC93X3) − 个人欠款(ATC93E)")
    w(6, 1, "说明：本次免税(ATC936) = 采暖费(ATC93W21) + 独生子女费(ATC93W4)，经数据库验证 100% 吻合；大病险个人(ATC93BD)同属免税。")
    w(7, 1, "说明：个人交纳现金(ATC93X3)=个人现金交纳的社保（如零工资月份补缴），计入本期收入、对应五险一金列抵扣；个人欠款(ATC93E)为欠款冲抵（零工资补缴月份为负值）。均与个税端回盘口径一致。")

    w(9, 1, "二、左（报税口径收入 − 个人扣减 + 本次免税）", bold=True)
    w(10, 1, "左 = 本期收入 − 当月养老个人缴(BAA001) − 当月失业个人缴(BAA003) − 当月医疗个人缴(BAA002) − 个人公积金月缴存额(CAA002) − 意外险个人(ATC93BH) + 本次免税(ATC936)")
    w(11, 1, "说明：本期收入内部已减本次免税(ATC936)与大病险个人(ATC93BD)，此处加回本次免税使左式回到工资总额口径，与实发侧对齐；")
    w(12, 1, "大病险个人(ATC93BD)左右两侧同项（收入侧已减、实发已刨除），销项不再单列；个人其他调整(ATC93AG)移至右式还原。")

    w(14, 1, "三、右（实发侧还原）", bold=True)
    w(15, 1, "右 = （本次实发金额合计(ATC93C) − 经济补偿金(ATC93M)） + 税后扣除工会会费(ATC93Z2) + 个人承担代理费(BAA300) + 本次个人所得税(ATC93D) + 个人其他调整(ATC93AG)")
    w(16, 1, "说明：实发金额已代扣工会会费、个税、个人其他调整，需加回复原；经济补偿金(ATC93M)是解除劳动合同一次性补偿，含在实发中但属一次性补偿收入，不参与正常工资薪金验算，故从实发扣回。")

    w(18, 1, "四、通俗理解", bold=True)
    w(19, 1, "左 = 应计入报税的收入 − 个人五险一金等扣减 + 本次免税；右 = 实际发放的钱 + 代扣的税费 + 个人其他调整。")
    w(20, 1, "两边从不同角度还原同一笔工资，应当相等；不相等则数据有疑点，需人工核对。")
    w(21, 1, "举例：某人工资总额 5000（其中含采暖费 500 免税），五险 800，个税 100，工会会费 0。")
    w(22, 1, "  左 = 本期收入(5000−500=4500) − 800 + 本次免税(500) = 4200")
    w(23, 1, "  实发 = 5000 − 800 − 100 = 4100（采暖费 500 已发到手里，含在实发里）")
    w(24, 1, "  右 = 实发(4100) + 工会会费(0) + 个税(100) + 其他调整(0) = 4200")
    w(25, 1, "  左 = 右 = 4200 ✓ 对上！")

    w(27, 1, "五、字段关系图", bold=True)
    rel_rows = [
        ("", "【收入侧】", ""),
        ("本次工资总额(ATC93AA)", "− 本次免税(ATC936)", "采暖费+独生子女费，免税不计收入"),
        ("", "− 大病险个人(ATC93BD)", "大病险个人部分同样免税"),
        ("", "− 补缴退款差额(ATC93BE) + 交纳现金(X3) − 个人欠款(E)", "补缴/退款冲抵；现金交纳与欠款冲抵均计入收入"),
        ("= 本期收入", "← 报税口径的收入", ""),
        ("本期收入", "− 养老(BAA001) − 失业(BAA003) − 医疗(BAA002) − 公积金(CAA002)", "五险一金个人缴"),
        ("", "− 意外险(ATC93BH) + 本次免税(ATC936)", "本次免税加回对齐实发；大病险个人销项"),
        ("= 左", "← 理论到手的钱", ""),
        ("", "", ""),
        ("", "【发放侧】", ""),
        ("本次实发金额(ATC93C)", "− 经济补偿金(ATC93M)", "一次性补偿，含在实发中，从实发扣回"),
        ("", "+ 税后工会会费(ATC93Z2)", "税后另扣的工会费，还原"),
        ("", "+ 个人代理费(BAA300)", "个人承担的代理费，还原"),
        ("", "+ 个人所得税(ATC93D)", "代扣的个税，还原"),
        ("", "+ 个人其他调整(ATC93AG)", "实发已扣的其他调整，还原"),
        ("= 右", "← 从实发反推的同一笔钱", ""),
    ]
    for i, (c1, c2, c3) in enumerate(rel_rows, 31):
        w(i, 1, c1)
        w(i, 2, c2)
        w(i, 3, c3)
    w(47, 1, "左 = 右（差值 < 0.01）→ 数据正确")

    w(49, 1, "六、验证报告列字段对照", bold=True)
    headers = ["验证列", "公式", "涉及字段"]
    for col, h in enumerate(headers, 1):
        w(50, col, h, bold=True)
    rows = [
        ("本期收入", "工资总额 − 本次免税 − 大病险个人 − 补缴及退款保险差额个人 + 个人交纳现金 − 个人欠款", "ATC93AA, ATC936, ATC93BD, ATC93BE, ATC93X3, ATC93E"),
        ("左", "本期收入 − 养老 − 失业 − 医疗 − 公积金 − 意外险 + 本次免税", "BAA001, BAA003, BAA002, CAA002, ATC93BH, ATC936"),
        ("右", "实发合计 − 经济补偿金 + 税后工会会费 + 个人代理费 + 个税 + 个人其他调整", "ATC93C, ATC93M, ATC93Z2, BAA300, ATC93D, ATC93AG"),
        ("经济补偿金", "本次经济补偿金(ATC93M)，从实发中扣回后参与右式", "ATC93M"),
        ("经济补偿是否达交税标准", "经济补偿金 > 3×年平均工资（默认120000）为已达，需单独按一次性补偿收入计税", "ATC93M, annual_avg_wage"),
        ("差值", "|左 − 右|", ""),
        ("状态", "差值 < 0.01 为通过", ""),
    ]
    for i, (c1, c2, c3) in enumerate(rows, 51):
        w(i, 1, c1)
        w(i, 2, c2)
        w(i, 3, c3)

    w(59, 1, "七、验算示例（第一条记录实际数值）", bold=True)
    if records:
        rec = records[0]
        income = calc_本期收入(rec)
        left = (income - rec.养老个人 - rec.失业个人 - rec.医疗个人 - rec.公积金个人
                - rec.意外险个人 + rec.补发3)
        right = ((rec.实发工资 - rec.经济补偿金) + rec.税后工会会费 + rec.个人代理费
                 + rec.个人所得税 + rec.个人其他调整)
        ex_rows = [
            ("姓名", rec.姓名, ""),
            ("工资总额(ATC93AA)", rec.工资总额, ""),
            ("本次免税(ATC936)", rec.补发3, ""),
            ("大病险个人(ATC93BD)", rec.大病险个人, "本期收入内已减、实发已刨除，销项"),
            ("补缴及退款保险差额个人(ATC93BE)", rec.补缴及退款保险金额个人, ""),
            ("本期收入", income, "工资总额 − 本次免税 − 大病险 − 补缴退款差额 + 个人交纳现金 − 个人欠款"),
            ("五险一金(养老+失业+医疗+公积金)", rec.养老个人 + rec.失业个人 + rec.医疗个人 + rec.公积金个人, "BAA001+BAA003+BAA002+CAA002"),
            ("意外险个人(ATC93BH)", rec.意外险个人, ""),
            ("左", left, "本期收入 − 五险一金 − 意外险 + 本次免税"),
            ("实发合计(ATC93C)", rec.实发工资, ""),
            ("经济补偿金(ATC93M)", rec.经济补偿金, "含在实发中，从实发扣回"),
            ("税后工会会费(ATC93Z2)", rec.税后工会会费, ""),
            ("个人代理费(BAA300)", rec.个人代理费, ""),
            ("个人所得税(ATC93D)", rec.个人所得税, ""),
            ("个人其他调整(ATC93AG)", rec.个人其他调整, ""),
            ("右", right, "实发 − 经济补偿金 + 工会会费 + 代理费 + 个税 + 其他调整"),
            ("差值", abs(left - right), "|左 − 右|，<0.01 通过"),
        ]
        for i, (name, val, note) in enumerate(ex_rows, 60):
            w(i, 1, name)
            w(i, 2, val)
            w(i, 3, note)

    w(79, 1, "八、东软数据库月份字段说明（重要）", bold=True)
    w(80, 1, "东软系统存在三个易混淆的月份字段，务必区分：")
    w(81, 1, "  ATC931 工资所属年月   — 工资属于哪个月（如6月工资=202606）")
    w(82, 1, "  ATC932 工资发放年月   — 工资实际发放的月份（TC93表内字段）")
    w(83, 1, "  ATC8G7 经办年月       — 发放年月的【最终依据】（TC8M表字段，本工具'发放月份'下拉即此字段）")
    w(84, 1, "本工具选择'发放月份'时，实际筛选的是 TC8M.ATC8G7（经办年月）。")
    w(85, 1, "同一组合(结算单元+所属月份+批次)下，TC93.ATC932 可能与 TC8M.ATC8G7 不一致：")
    w(86, 1, "如 单元37185 所属202606批1 的 TC93.ATC932=202606（6月发放），但 TC8M.ATC8G7=202607（7月经办申报）。")
    w(87, 1, "因此 TC93总表 sheet 中会出现'发放年月(ATC932)=202606'而您选择发放月份202607的记录——这是正常的，")
    w(88, 1, "请以 TC93总表 最后列的'经办年月(ATC8G7)'为准判断该笔工资属于哪个申报期。")


def generate_field_mapping_sheet(wb: Workbook):
    """字段对应关系sheet：按实际工资表（tddd_dialog2 类 57 列劳务派遣工资表）逐列映射。

    六列：序号 | 区块 | 工资表薪资明细名称 | 数据库字段名称 | 数据库字段注释 | 备注。
    - 第3列薪资明细名称、第4列数据库字段、第5列字段注释为主框架；
    - 字段注释严格采用数据库 ALL_COL_COMMENTS 原注释；
    - TC8M 等表的批次级字段一并列出，无工资表对应列的，薪资明细名称列留空；
    - 映射已经 202608 全量 881 行与 TC93 逐列数值比对实证（匹配行数列于备注）。
    """
    ws = wb.create_sheet("字段对应关系")
    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 26
    ws.column_dimensions["D"].width = 20
    ws.column_dimensions["E"].width = 30
    ws.column_dimensions["F"].width = 55
    headers = ["序号", "区块", "工资表薪资明细名称", "数据库字段名称", "数据库字段注释", "备注"]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)
    rows = [
        # ---- 基本信息 (col0-5) ----
        (1, "基本信息", "序号", "", "", "工资表行序号，无对应数据库字段"),
        (2, "基本信息", "姓名", "AC01.AAC003", "姓名", ""),
        (3, "基本信息", "身份证", "AC01.AAC002", "身份证号", "TC93.AAC002 同义（公民身份号码）；经 AC01 关联获取"),
        (4, "基本信息", "部门", "TC93.AAB004_1", "部门", ""),
        (5, "基本信息", "岗位", "TC93.ATC908", "岗位", ""),
        (6, "基本信息", "职工号", "TC93.AAE00B", "员工号", ""),
        # ---- 收入细项 (col6-22) ----
        (7, "收入细项", "扣款", "TC93.ATC934", "本次扣款", "全量比对非零 8 行全部吻合"),
        (8, "收入细项", "奖金", "TC93.ATC93W1", "奖金", "非零 850 行吻合"),
        (9, "收入细项", "岗位工资", "TC93.ATC93W9", "岗位工资", "非零 880 行吻合"),
        (10, "收入细项", "绩效奖金", "TC93.ATC93W10", "绩效奖金", "非零 474 行吻合"),
        (11, "收入细项", "一次性奖金", "TC93.ATC93W11", "一次性奖金", ""),
        (12, "收入细项", "津贴", "TC93.ATC93W12", "津贴", ""),
        (13, "收入细项", "合计夜餐费", "TC93.ATC93W13", "合计夜餐费", ""),
        (14, "收入细项", "合计加班费", "TC93.ATC93W14", "合计加班费", ""),
        (15, "收入细项", "补发", "TC93.ATC93W15", "补发", ""),
        (16, "收入细项", "补贴2（餐现）", "TC93.ATC93W16", "补贴2（餐现）", ""),
        (17, "收入细项", "补发（体检费）", "TC93.ATC93W17", "补发（体检费）", ""),
        (18, "收入细项", "病假扣款", "TC93.ATC93W18", "病假扣款", ""),
        (19, "收入细项", "事假扣款", "TC93.ATC93W19", "事假扣款", ""),
        (20, "收入细项", "大众班车费（扣）", "TC93.ATC93W20", "大众班车费（扣）", ""),
        (21, "收入细项", "补贴", "TC93.ATC93W24", "补贴", "非零 427 行吻合"),
        (22, "收入细项", "交通补贴", "TC93.ATC93W35", "交通补贴", "源表第21列表头'交通补贴'，数据实际在第22列（无表头列）"),
        (23, "收入细项", "", "TC93.ATC93W35", "交通补贴", "源表第22列无表头，存放交通补贴数据（与第21列表头错位，归入上一行）"),
        # ---- 应发与单位缴纳 (col23-29) ----
        (24, "应发与单位缴纳", "应发工资", "TC93.ATC93AA", "本次工资总额", "工资表列名'应发工资'，数据库注释为'本次工资总额'，非零 874 行吻合；库中 ATC933'本次应发工资'基本未使用（202608 全 0）"),
        (25, "应发与单位缴纳", "单位缴纳五险一金", "TC93.BBA200", "当月单位险金合计", "非零 864 行吻合"),
        (26, "应发与单位缴纳", "补缴及退款保险差额（单位）", "TC93.ATC93BA", "补缴及退款保险差额（单位）", "非零 9 行吻合"),
        (27, "应发与单位缴纳", "单位代理费", "TC93.ATB90A", "单位代理费", "转账合计区；全量数值与 ATC93Y3（扣款-单位代理费）恒等，按所在区块语义区分"),
        (28, "应发与单位缴纳", "返餐费", "TC93.ATC93X1", "返还单位-加班餐费", "非零 742 行吻合"),
        (29, "应发与单位缴纳", "意外险", "TC93.ATC93BB", "意外险（单位承担）", "非零 13 行吻合；ATC93BH（个人承担）202608 全为 0"),
        (30, "应发与单位缴纳", "转账合计", "TC93.ATC93BF", "转款合计", "非零 880 行吻合"),
        # ---- 缴费基数 (col30-33) ----
        (31, "缴费基数", "社保基数", "TC93.AAA301", "当月社保缴费基数", ""),
        (32, "缴费基数", "医保基数", "TC93.AAA302", "当月医保缴费基数", ""),
        (33, "缴费基数", "工伤基数", "TC93.AAA305", "工伤缴费基数", ""),
        (34, "缴费基数", "公积金基数", "TC93.AAA303", "当月公积金缴费基数", ""),
        # ---- 扣款明细 (col34-50) ----
        (35, "扣款明细", "养老(单位)", "TC93.BBA001", "当月养老单位缴", "非零 864 行吻合"),
        (36, "扣款明细", "养老(个人)", "TC93.BAA001", "当月养老个人缴", "非零 864 行吻合"),
        (37, "扣款明细", "失业(单位)", "TC93.BBA003", "当月失业单位缴", ""),
        (38, "扣款明细", "失业(个人)", "TC93.BAA003", "当月失业个人缴", ""),
        (39, "扣款明细", "医疗(单位)", "TC93.BBA002", "当月医疗单位缴", ""),
        (40, "扣款明细", "医疗(个人)", "TC93.BAA002", "当月医疗个人缴", ""),
        (41, "扣款明细", "工伤险(单位)", "TC93.BBA004", "当月工伤单位缴", ""),
        (42, "扣款明细", "公积金(单位)", "TC93.CAA001", "当月单位公积金月缴存额", ""),
        (43, "扣款明细", "公积金(个人)", "TC93.CAA002", "个人公积金月缴存额", ""),
        (44, "扣款明细", "补缴及退款保险差额（单位）", "TC93.ATC93BA", "补缴及退款保险差额（单位）", "与应发区同字段重复列"),
        (45, "扣款明细", "补缴及退款保险差额（个人）", "TC93.ATC93BE", "补缴及退款保险差额（个人）", ""),
        (46, "扣款明细", "大病险（个人）", "TC93.ATC93BD", "大病险（个人承担）", ""),
        (47, "扣款明细", "大病险合计", "TC93.ATC93Y2", "扣款-大病险", "Y2 = BD + BC（单位承担），202608 BC 全 0 故与 BD 相等"),
        (48, "扣款明细", "单位代理费", "TC93.ATC93Y3", "扣款-单位代理费", "与应发区 ATB90A 数值恒等，按区块语义区分"),
        (49, "扣款明细", "意外险", "TC93.ATC93BB", "意外险（单位承担）", "与应发区同字段"),
        (50, "扣款明细", "返餐费", "TC93.ATC93X1", "返还单位-加班餐费", "与应发区同字段"),
        (51, "扣款明细", "扣款合计", "TC93.ATC93YZ", "扣款合计", "全量 881 行全部吻合"),
        # ---- 税款与实发 (col51-56) ----
        (52, "税款与实发", "应纳税所得额", "TC93.ATC93AB", "累计应税金额", "工资表列名'应纳税所得额'，数据库注释为'累计应税金额'，全量 881 行完全吻合；非本月应税额度(ATC9P3)"),
        (53, "税款与实发", "个人所得税", "TC93.ATC93D", "本次个人所得税", ""),
        (54, "税款与实发", "个人欠款", "TC93.ATC93E", "个人欠款", ""),
        (55, "税款与实发", "实发工资", "TC93.ATC93Z1", "实发工资", "非零 874 行吻合"),
        (56, "税款与实发", "个人其他调整", "TC93.ATC93AG", "个人其他调整", "非零 500 行吻合"),
        (57, "税款与实发", "实发合计", "TC93.ATC93C", "本次实发金额(合计)", "非零 874 行吻合"),
        # ---- TC8M/TC93 批次级字段（工资表无对应列，名称留空）----
        (58, "TC8M等表", "", "TC8M.ATC8G7", "经办年月", "发放月份下拉即此字段，以此判断申报期；与 TC93.ATC932 工资发放年月、ATC931 工资所属年月易混淆"),
        (59, "TC8M等表", "", "TC8M.ATC8M0", "主键", "TC8M 主键"),
        (60, "TC8M等表", "", "TC8M.ATC8M1", "发放人数", "批次发放人数"),
        (61, "TC8M等表", "", "TC8M.ATC8M2", "发放总额", "批次发放总额"),
        (62, "TC8M等表", "", "TC8M.ATC8M3", "工资状态", "2=正常，用于过滤异常批次"),
        (63, "TC8M等表", "", "TC8M.ATB930", "结算单元流水号", "TC93 亦有同名字段"),
        (64, "TC8M等表", "", "TC8M.ATB931", "结算单元名称", "TC93 亦有同名字段"),
        (65, "TC93相关", "", "TC93.AAC001", "个人编号", "TC93 与 AC01 关联键"),
        (66, "TC93相关", "", "TC93.ATC930", "流水号", "TC93 主键流水号"),
        (67, "TC93相关", "", "TC93.ATC931", "工资所属年月", "工资属于哪个月"),
        (68, "TC93相关", "", "TC93.ATC932", "工资发放年月", "TC93 表内字段"),
        (69, "TC93相关", "", "TC93.ATC937", "工资发放次数", "批次"),
    ]
    for i, (c1, c2, c3, c4, c5, c6) in enumerate(rows, 2):
        ws.cell(row=i, column=1, value=c1)
        ws.cell(row=i, column=2, value=c2)
        ws.cell(row=i, column=3, value=c3)
        ws.cell(row=i, column=4, value=c4)
        ws.cell(row=i, column=5, value=c5)
        ws.cell(row=i, column=6, value=c6)


def generate_raw_detail_sheet(wb: Workbook, raw_records: List[SalaryRecord],
                              combo_map: dict, title: str):
    """原始明细(未合并)sheet：逐条列出未合并记录的报送数据与验算，保证可追溯。"""
    ws = wb.create_sheet("原始明细(未合并)")
    headers = [
        "ATC930", "姓名", "证件号码", "结算单元", "所属月份", "批次",
        "工资总额(AA)", "本次免税(936)", "大病险(BD)", "补缴退款差额(BE)", "交纳现金(X3)", "本期收入",
        "养老(BAA001)", "失业(BAA003)", "医疗(BAA002)", "公积金(CAA002)",
        "意外险(BH)", "个人欠款(E)", "左",
        "实发(C)", "工会会费(Z2)", "代理费(BAA300)", "个税(D)", "其他调整(AG)",
        "右", "差值", "状态", "备注"
    ]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)
    for idx, rec in enumerate(raw_records, 2):
        income = calc_本期收入(rec)
        left = (income - rec.养老个人 - rec.失业个人 - rec.医疗个人 - rec.公积金个人
                - rec.意外险个人 + rec.补发3)
        right = ((rec.实发工资 - rec.经济补偿金) + rec.税后工会会费 + rec.个人代理费
                 + rec.个人所得税 + rec.个人其他调整)
        diff = abs(left - right)
        remark = str(rec.结算单元名称 or "").strip() or title
        vals = [
            rec.tc930_id, rec.姓名, rec.身份证, rec.结算单元, rec.工资所属年月, rec.当月批次,
            rec.工资总额, rec.补发3, rec.大病险个人, rec.补缴及退款保险金额个人, rec.个人交纳现金, income,
            rec.养老个人, rec.失业个人, rec.医疗个人, rec.公积金个人,
            rec.意外险个人, rec.个人欠款, left,
            rec.实发工资, rec.税后工会会费, rec.个人代理费, rec.个人所得税, rec.个人其他调整,
            right, round(float(diff), 4), "通过" if diff < 0.01 else "失败", remark
        ]
        for col, val in enumerate(vals, 1):
            ws.cell(row=idx, column=col, value=val)


def generate_merge_detail_sheet(wb: Workbook, raw_records: List[SalaryRecord],
                                merged_records: List[SalaryRecord],
                                merge_mode: str = "month"):
    """合并明细sheet：按人(+所属月份)分组，展示原始条目与合并后汇总，合并过程可追溯。

    merge_mode="month":     按人+所属月份分组（现状）
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
        "原始记录(ATC930/本期收入/个税)", "合并后结算单元", "合并后批次",
        "合并本期收入", "合并五险", "合并个税", "合并实发", "合并本次免税(936)",
        "合并左", "合并右", "差值", "状态"
    ]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    if by_pay_month:
        merged_map = {m.职工号: m for m in merged_records}
    else:
        merged_map = {(m.职工号, m.工资所属年月): m for m in merged_records}

    def _merged_sort_key(kv):
        m = merged_map.get(kv[0][0] if by_pay_month else kv[0])
        unit = str(getattr(m, "结算单元名称", "") or "") if m else ""
        return (unit, kv[0][0])

    ordered = sorted(groups.items(), key=_merged_sort_key)
    row = 2
    for key, recs in ordered:
        m = merged_map.get(key[0] if by_pay_month else key)
        if m is None:
            continue
        raw_desc = "; ".join(
            f"{r.tc930_id}/{round(float(calc_本期收入(r)), 2)}/{r.个人所得税}" for r in recs)
        month_display = (";".join(str(x) for x in sorted({r.工资所属年月 for r in recs}))
                         if by_pay_month else key[1])
        income = calc_本期收入(m)
        left = (income - m.养老个人 - m.失业个人 - m.医疗个人 - m.公积金个人
                - m.意外险个人 + m.补发3)
        right = ((m.实发工资 - m.经济补偿金) + m.税后工会会费 + m.个人代理费
                 + m.个人所得税 + m.个人其他调整)
        diff = abs(left - right)
        vals = [
            recs[0].姓名, recs[0].身份证, month_display, len(recs),
            raw_desc, m.结算单元, m.当月批次,
            income, m.养老个人 + m.失业个人 + m.医疗个人 + m.公积金个人, m.个人所得税,
            m.实发工资, m.补发3,
            left, right, round(float(diff), 4), "通过" if diff < 0.01 else "失败"
        ]
        for col, val in enumerate(vals, 1):
            ws.cell(row=row, column=col, value=val)
        row += 1


def _build_person_name_map(records, raw_records, tc93_all, abnormal) -> dict:
    """构建 证件号->姓名 映射，供合并/零申报验证 sheet 显示人员姓名。

    合并后记录优先（多来源合并时姓名取合并结果），缺失时从原始记录/TC93总表/异常表补齐。
    """
    name_of = {}
    for r in list(records or []) + list(raw_records or []):
        cert = str(getattr(r, "身份证", None) or getattr(r, "职工号", None) or "")
        if cert and cert not in name_of:
            name_of[cert] = getattr(r, "姓名", None) or ""
    for d in list(tc93_all or []) + list(abnormal or []):
        cert = str(d.get("身份证") or d.get("AAC001") or "")
        if cert and cert not in name_of:
            name_of[cert] = d.get("姓名") or d.get("AAC003") or ""
    return name_of


def generate_merge_verification_sheet(wb: Workbook, merge_choices: dict,
                                      name_of: dict, persist: bool = False):
    """合并验证sheet：记录用户对跨月合并人员三险一金合并方式的逐人确认过程。

    merge_choices: {证件号: 'double'|'single'|'skip'}，来自合并规则确认弹窗。
    """
    ws = wb.create_sheet("合并验证")
    headers = ["证件号码", "姓名", "用户确认方式", "生效口径", "本次已记住"]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)
    mode_label = {
        "double": "多月合并",
        "single": "单月",
        "skip": "不报",
    }
    mode_rule = {
        "double": "三险一金跨所属月全部合计（默认口径）",
        "single": "三险一金只取最近一次三险>0的所属月（发放月有三险则=发放月）",
        "skip": "三险一金全按0上报（仅发放月无三险时生效，发放月有三险时自动回落多月合并）",
    }
    for idx, (cert, mode) in enumerate(sorted(merge_choices.items()), 2):
        vals = [cert, name_of.get(cert, ""), mode_label.get(mode, mode),
                mode_rule.get(mode, ""), "是" if persist else "否"]
        for col, val in enumerate(vals, 1):
            ws.cell(row=idx, column=col, value=val)
    for col, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(col)].width = max(12, len(str(h)) * 2)


def generate_zero_verification_sheet(wb: Workbook, zero_choices: dict,
                                     records: List[SalaryRecord], name_of: dict,
                                     persist: bool = False):
    """零申报验证sheet：记录用户对本期收入为0人员是否生成零申报的逐人确认过程。

    zero_choices: {证件号: 'declare'|'skip'}
    """
    ws = wb.create_sheet("零申报验证")
    headers = ["证件号码", "姓名", "用户确认方式", "实际结果", "本次已记住"]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)
    final_certs = {str(r.身份证 or r.职工号 or "").strip().upper() for r in (records or [])}
    for idx, (cert, mode) in enumerate(sorted(zero_choices.items()), 2):
        c = str(cert or "").strip().upper()
        if mode == "skip":
            result = "未生成（已跳过，不在本文件收入表中）"
        elif c in final_certs:
            result = "已生成（零申报记录保留/注入，含于本文件收入表）"
        else:
            result = "未生成（受特殊结算单元排除等规则过滤）"
        vals = [cert, name_of.get(cert, ""),
                "生成零申报" if mode == "declare" else "不生成(跳过)",
                result, "是" if persist else "否"]
        for col, val in enumerate(vals, 1):
            ws.cell(row=idx, column=col, value=val)
    for col, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(col)].width = max(12, len(str(h)) * 2)