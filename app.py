"""Flask 主应用 - 个税模板填表工具"""
import atexit
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, request, jsonify, send_file
from db import init_db, get_connection, close_db
from queries import get_available_months, get_salary_records, get_personnel_info, get_suggestions, search_tc8m, get_abnormal_records, get_tc93_all_fields, get_tc93_field_comments, get_merge_warnings, get_pay_months, get_payroll_cert_numbers, get_tc90_salary_end_dates, get_payroll_personnel, get_labor_service_cert_numbers
from templates_gen.normal_salary import generate_normal_salary, generate_tc93_full_sheet, generate_abnormal_sheet
from templates_gen.labor_service import generate_labor_service
from templates_gen.annual_bonus import generate_annual_bonus
from templates_gen.personnel_info import generate_personnel_info
from templates_gen.validation import validate_salary_records
from tax_merge import merge_records_by_person, build_merge_suggestions
from tax_zero import build_zero_salary_suggestions, filter_zero_records, filter_zero_dicts

app = Flask(__name__)
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 错误日志落盘 (logs/taxgen.log, 5MB×3 轮转), 后台定位问题用
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "taxgen.log"), maxBytes=5 * 1024 * 1024,
    backupCount=3, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
logging.getLogger().addHandler(_file_handler)
logging.getLogger().setLevel(logging.INFO)

# 注意: 连接池在进程生命周期内保持打开 (oracledb 连接在 GC 时自动归还池),
# 仅在进程退出时关闭。不能在 teardown_appcontext 中调用 close_db(),
# 否则每个请求后池被关闭, 后续所有数据库请求都会失败。
atexit.register(close_db)


def _log_api_error(e):
    """记录 API 异常堆栈到日志文件并返回统一错误响应。"""
    logging.getLogger(__name__).exception("API error: %s", e)
    return jsonify({"error": f"处理失败: {e}"}), 500


def _apply_scope_filter(combo_set, scope_map):
    """按结算单元 salary_month_scope 配置过滤组合集合。

    默认 'all' 保留全部所属月(多月合并); 'latest_N' 仅保留所属月最大的 N 个月,
    'first_N' 仅保留所属月最小的 N 个月。非法配置回退 'all' 全保留, 避免丢数据。
    """
    if not scope_map:
        return combo_set
    unit_months = {}
    for unit, sm, _seq in combo_set:
        unit_months.setdefault(unit, set()).add(sm)
    kept = set()
    for unit, sm, seq in combo_set:
        scope = scope_map.get(unit, "all")
        if scope == "all":
            kept.add((unit, sm, seq))
            continue
        parts = scope.split("_")
        if len(parts) != 2 or parts[0] not in ("first", "latest"):
            kept.add((unit, sm, seq))
            continue
        try:
            n = int(parts[1])
        except ValueError:
            kept.add((unit, sm, seq))
            continue
        if n <= 0:
            kept.add((unit, sm, seq))
            continue
        months = sorted(unit_months.get(unit, set()))
        selected = months[:n] if parts[0] == "first" else months[-n:]
        if sm in selected:
            kept.add((unit, sm, seq))
    return kept


def build_labor_service_records(conn, raw_records, confirmed_combos, month,
                                merge_by_person, merge_by_pay_month, zero_codes, excl_codes):
    """劳务报酬模板数据: 按发放月(ATC8G7) + 当前有效TC90 AAE00P=3 过滤。

    口径对齐手工「07-月劳务报酬所得」文件 (258/258 精确匹配验证):
    发放月份=TC8M.ATC8G7 且 ATC8M3='2'; 同一发放月横跨多个所属月
    (ATC931), 收入=各所属月 ATC93AA 之和; 仅保留 AAE00P='3'(劳务报酬)。
    有 confirmed_combos 时复用按批次过滤的原始记录, 否则按发放月自动取全部
    已确认批次 (与 Web 组合流程等价)。AAE00P 按身份证集合统一大写过滤。

    返回 (合并后记录, 合并前原始记录, 使用的组合列表) 供辅助 sheet 使用。
    """
    from queries import get_suggestions, get_salary_records, get_labor_service_cert_numbers
    combos = confirmed_combos
    if combos:
        lab_raw = list(raw_records)
    else:
        combos = get_suggestions(conn, month)
        combo_set = {(c["unit"], c["salary_month"], c["seq"]) for c in combos}
        salary_months = {c["salary_month"] for c in combos}
        lab_raw = []
        for sm in sorted(salary_months):
            lab_raw.extend(get_salary_records(conn, sm))
        lab_raw = [r for r in lab_raw
                   if (r.结算单元, r.工资所属年月, r.当月批次) in combo_set]
        if zero_codes or excl_codes:
            def _keep_lab(unit, salary_total):
                if unit in excl_codes:
                    return False
                if unit in zero_codes and (salary_total or 0) == 0:
                    return False
                return True
            lab_raw = [r for r in lab_raw if _keep_lab(r.结算单元, r.工资总额)]
    lab_certs = get_labor_service_cert_numbers(conn, month)
    if lab_certs:
        lab_raw = [r for r in lab_raw if r.身份证 and r.身份证.strip().upper() in lab_certs]
    if merge_by_person:
        lab_records = merge_records_by_person(lab_raw, by_pay_month=merge_by_pay_month)
    else:
        lab_records = list(lab_raw)
    # 手工「07-月劳务报酬所得」文件 258 人精确匹配验证: 仅含工资总额>0 的人员
    # (工资为0 不发钱, 不应出现在劳务报酬申报模板)
    lab_records = [r for r in lab_records if r.工资总额 and float(r.工资总额) > 0]
    return lab_records, lab_raw, combos


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/months")
def api_months():
    try:
        conn = get_connection()
        months = get_available_months(conn)
        return jsonify([{"value": m.value, "label": m.label} for m in months])
    except Exception as e:
        return _log_api_error(e)

@app.route("/api/suggestions/<int:month>")
def api_suggestions(month):
    try:
        conn = get_connection()
        combos = get_suggestions(conn, month)

        return jsonify({
            "month": month,
            "combos": [{
                "unit": c["unit"],
                "unit_name": c["unit_name"],
                "salary_month": c["salary_month"],
                "seq": c["seq"],
                "person_count": c["person_count"],
                "total_income": round(c["total_income"], 2)
            } for c in combos],
        })
    except Exception as e:
        return _log_api_error(e)

@app.route("/api/tc8m/search")
def api_tc8m_search():
    try:
        conn = get_connection()
        unit_name = request.args.get("unit_name", "").strip()
        salary_month = int(request.args.get("salary_month", 0) or 0)
        pay_month = int(request.args.get("pay_month", 0) or 0)
        seq = request.args.get("seq", "").strip()
        status = int(request.args.get("status", -1) or -1)
        handler = request.args.get("handler", "").strip()
        results = search_tc8m(conn, unit_name, salary_month, pay_month, seq, status, handler)
        return jsonify({"results": results})
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/merge-suggestions", methods=["POST"])
def api_merge_suggestions():
    """生成前确认: 扫描待报组合中的跨月合并人员，给出多月/单月判定建议。"""
    try:
        data = request.get_json()
        pay_month = int(data.get("pay_month") or 0)
        combos = data.get("combos") or []
        if not pay_month or not combos:
            return jsonify({"error": "请选择月份并勾选待报组合"}), 400
        if any(not c.get("seq") for c in combos):
            return jsonify({"error": "组合缺少批次号"}), 400
        conn = get_connection()
        return jsonify(build_merge_suggestions(conn, pay_month, combos))
    except Exception as e:
        return _log_api_error(e)

@app.route("/api/zero-suggestions", methods=["POST"])
def api_zero_suggestions():
    """生成前确认: 扫描待报组合中本期收入=0 的人员，给出零申报生成/不生成建议。"""
    try:
        data = request.get_json()
        pay_month = int(data.get("pay_month") or 0)
        combos = data.get("combos") or []
        if not pay_month or not combos:
            return jsonify({"error": "请选择月份并勾选待报组合"}), 400
        if any(not c.get("seq") for c in combos):
            return jsonify({"error": "组合缺少批次号"}), 400
        conn = get_connection()
        return jsonify(build_zero_salary_suggestions(conn, pay_month, combos))
    except Exception as e:
        return _log_api_error(e)

@app.route("/api/merge-suggestions/export", methods=["POST"])
def api_merge_suggestions_export():
    """导出合并确认候选为 Excel: 按工资单分组行, 含建议/选择列, 供 Excel 中批量修改后导回。"""
    try:
        data = request.get_json()
        pay_month = int(data.get("pay_month") or 0)
        combos = data.get("combos") or []
        if not pay_month or not combos:
            return jsonify({"error": "请选择月份并勾选待报组合"}), 400
        conn = get_connection()
        res = build_merge_suggestions(conn, pay_month, combos)
        return _merge_suggestions_to_xlsx(res)
    except Exception as e:
        return _log_api_error(e)


def _merge_suggestions_to_xlsx(res: dict):
    """把合并建议结果渲染为 xlsx (BytesIO), 返回 send_file 响应。

    每人一行 (与合并确认粒度一致), 工资单列列出该人全部 (结算单元-所属月-批次),
    可按工资单列排序/筛选后在 Excel 整批修改"选择"列, 再导回。
    """
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "合并确认"
    headers = ["工资单(结算单元-所属月-批次)", "姓名", "证件号", "职工号", "主结算单元",
               "上月状态", "建议", "置信度", "理由",
               "本期收入合计", "三险-多月", "三险-单月", "选择(单月/多月/不报)"]
    ws.append(headers)
    ws.freeze_panes = "A2"
    head_fill = PatternFill("solid", fgColor="DDEBF7")
    head_font = Font(bold=True)
    for c, _ in enumerate(headers, 1):
        cell = ws.cell(1, c)
        cell.fill = head_fill
        cell.font = head_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    slip_key = lambda s: f"{s['unit_name'] or s['unit']}({s['unit']})·{s['salary_month']}·批次{s['seq']}"
    main_name = lambda c: f"{c.get('main_unit_name') or c.get('main_unit')}({c.get('main_unit')})"
    for g in res.get("work_sheets", []):
        for c in g.get("persons", []):
            slips = [slip_key(s) for s in c.get("slips", [])]
            ws.append([
                "；".join(slips),
                c.get("name", ""),
                c["cert_no"],
                c.get("emp_no", ""),
                main_name(c),
                c.get("prev_status", ""),
                "多月" if c.get("suggested") == "double" else "单月",
                {"high": "高", "medium": "中", "low": "低"}.get(c.get("confidence"), ""),
                c.get("reason", ""),
                c.get("income_total", 0),
                c.get("insurance_double", 0),
                c.get("insurance_single", 0),
                {"double": "多月", "single": "单月", "skip": "不报"}.get(
                    c.get("default_chosen", c.get("suggested")), "单月"),
            ])

    width_map = {"A": 55, "B": 10, "C": 20, "D": 10, "E": 24, "F": 8,
                 "G": 6, "H": 6, "I": 40, "J": 12, "K": 12, "L": 12, "M": 12}
    for col, w in width_map.items():
        ws.column_dimensions[col].width = w
    ws.auto_filter.ref = f"A1:M{ws.max_row}"

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    fname = f"合并确认_{res.get('pay_month', '')}.xlsx"
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/api/merge-suggestions/import", methods=["POST"])
def api_merge_suggestions_import():
    """导回合并确认 Excel: 解析"选择(单月/多月/不报)"列, 按证件号返回 {cert_no: mode}。"""
    try:
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "请选择要导入的 Excel 文件"}), 400
        from openpyxl import load_workbook
        wb = load_workbook(f, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return jsonify({"error": "文件为空"}), 400
        header = [str(h or "").strip() for h in rows[0]]
        mode_col = next((h for h in header if h in ("选择(单月/多月/不报)", "选择(单月/多月)", "选择(当月/多月)", "选择(翻倍/单倍)")), None)
        if "证件号" not in header or not mode_col:
            return jsonify({"error": "列不匹配: 需要 证件号 与 选择(单月/多月/不报) 列（请用本系统导出的 Excel 修改；旧版列名 选择(单月/多月)/选择(当月/多月)/选择(翻倍/单倍) 也支持）"}), 400
        i_cert = header.index("证件号")
        i_mode = header.index(mode_col)
        choices = {}
        for r in rows[1:]:
            cert = str(r[i_cert] or "").strip()
            mode_raw = str(r[i_mode] or "").strip()
            if not cert or not mode_raw:
                continue
            if "多月" in mode_raw or "翻倍" in mode_raw:
                mode = "double"
            elif "单月" in mode_raw or "当月" in mode_raw or "单倍" in mode_raw:
                mode = "single"
            elif "不报" in mode_raw or "跳过" in mode_raw:
                mode = "skip"
            else:
                mode = ""
            if mode:
                choices[cert] = mode
            elif cert:
                return jsonify({"error": f"证件号 {cert} 的选择列含无法识别值: '{mode_raw}'（仅支持 单月/多月/不报）"}), 400
        if not choices:
            return jsonify({"error": "未解析到任何有效的选择记录"}), 400
        return jsonify({"count": len(choices), "choices": choices})
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/merge-group-tags", methods=["GET"])
def api_merge_group_tags_get():
    """获取合并确认界面的组标签映射 (纯标记提醒, SQLite config_db)。"""
    try:
        from config_db import get_merge_group_tags
        return jsonify({"tags": get_merge_group_tags()})
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/merge-group-tags", methods=["POST"])
def api_merge_group_tags_set():
    """保存某结算单元的组标签 (整体覆盖, 纯标记提醒, 不影响任何合并/生成逻辑, SQLite)。"""
    try:
        from config_db import set_merge_group_tags
        data = request.get_json() or {}
        unit_code = int(data.get("unit_code", 0) or 0)
        tags = data.get("tags") or []
        if not unit_code:
            return jsonify({"error": "缺少结算单元代码"}), 400
        if not isinstance(tags, list):
            return jsonify({"error": "tags 必须为字符串列表"}), 400
        set_merge_group_tags(unit_code, [str(t) for t in tags])
        return jsonify({"ok": True})
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/generate", methods=["POST"])
def api_generate():
    try:
        data = request.get_json()
        month = data.get("month")
        templates = data.get("templates", [])
        confirmed_combos = data.get("confirmed_combos")
        
        if not month:
            return jsonify({"error": "请选择月份"}), 400
        if not templates:
            return jsonify({"error": "请选择至少一个模板"}), 400
        
        conn = get_connection()
        if confirmed_combos is not None:
            combo_set = {(c["unit"], c["salary_month"], c["seq"]) for c in confirmed_combos}
            from config_db import get_scope_map
            combo_set = _apply_scope_filter(combo_set, get_scope_map())
            salary_months = {c[1] for c in combo_set}
            records = []
            for sm in salary_months:
                records.extend(get_salary_records(conn, sm))
            records = [r for r in records if (r.结算单元, r.工资所属年月, r.当月批次) in combo_set]
            tc93_all = []
            abnormal = []
            for sm in salary_months:
                tc93_all.extend(r for r in get_tc93_all_fields(conn, sm)
                                if (r.get("ATB930"), r.get("ATC931"), r.get("ATC937")) in combo_set)
                abnormal.extend(r for r in get_abnormal_records(conn, sm)
                                if (r.get("ATB930"), r.get("ATC931"), r.get("ATC937")) in combo_set)
            personnel = []
            for sm in salary_months:
                personnel.extend(get_personnel_info(conn, sm))
        else:
            records = get_salary_records(conn, month)
            personnel = get_personnel_info(conn, month)
            tc93_all = get_tc93_all_fields(conn, month)
            abnormal = get_abnormal_records(conn, month)
        merge_by_person = data.get("merge_by_person", True)
        merge_by_pay_month = bool(data.get("merge_by_pay_month", True))
        # 年平均工资总额（解除劳动合同一次性补偿金 3 倍免税判断基准，默认 12 万）
        annual_avg_wage = float(data.get("annual_avg_wage") or 120000)
        # 特殊结算单元规则: 工资为0不申报 + 完全排除不申报
        # 配置存 SQLite (config_db), Oracle 只读
        from config_db import get_zero_salary_unit_codes, get_excluded_unit_codes
        zero_codes = set(get_zero_salary_unit_codes())
        excl_codes = set(get_excluded_unit_codes())
        zero_choices = data.get("zero_choices") or {}
        if zero_choices:
            # 用户已通过零申报确认面板明确选择: 以面板选择取代配置自动排除
            # (包含"工资为0不申报"配置单元的可反转恢复); excl_codes 恒排除。
            from config_db import upsert_zero_overrides as _persist_zero_choices
            records = filter_zero_records(records, zero_choices, excl_codes)
            tc93_all = filter_zero_dicts(tc93_all, zero_choices, excl_codes)
            abnormal = filter_zero_dicts(abnormal, zero_choices, excl_codes)
            if data.get("persist_zero_choices") and zero_choices:
                _persist_zero_choices({c: m for c, m in zero_choices.items()
                                       if m in ("declare", "skip")})
        elif zero_codes or excl_codes:
            def _keep(unit, salary_total):
                if unit in excl_codes:
                    return False
                if unit in zero_codes and (salary_total or 0) == 0:
                    return False
                return True
            records = [r for r in records if _keep(r.结算单元, r.工资总额)]
            tc93_all = [r for r in tc93_all if _keep(r.get("ATB930"), r.get("ATC93AA"))]
            abnormal = [r for r in abnormal if _keep(r.get("ATB930"), r.get("ATC93AA"))]
        raw_records = records
        if merge_by_person:
            from config_db import upsert_merge_overrides
            merge_choices = data.get("merge_choices") or {}
            single_certs = {c for c, mode in merge_choices.items() if mode == "single"}
            # "不报"(skip)数据判定 (2026-09-10 用户确认, 开关已移除):
            # 仅当发放月(month)无三险一金才允许不报
            # (发放月记录三险全为 0 或 无发放月记录; 防御 Excel 导回等绕过 UI 的路径)
            skip_certs = set()
            for c, mode in merge_choices.items():
                if mode != "skip":
                    continue
                recs_c = [r for r in records if (r.身份证 or r.职工号) == c]
                cur_ins = sum(r.养老个人 + r.医疗个人 + r.失业个人 + r.公积金个人
                              for r in recs_c if r.工资所属年月 == month)
                if cur_ins == 0:
                    skip_certs.add(c)
            records = merge_records_by_person(records, by_pay_month=merge_by_pay_month,
                                              single_certs=single_certs,
                                              skip_certs=skip_certs, cur_month=month)
            if data.get("persist_merge_choices") and merge_choices:
                upsert_merge_overrides({c: m for c, m in merge_choices.items()
                                        if m in ("double", "single", "skip")})
        warnings = []
        if confirmed_combos and merge_by_person:
            persons = list({r.职工号 for r in raw_records})
            warnings = get_merge_warnings(conn, [month],
                                          combo_set if confirmed_combos else set(), persons)
        abnormal_reasons = {r.get("ATC930"): f"ATC93G={r.get('ATC93G', 'NULL')}(未结算)" for r in abnormal}
        
        results = []
        # 主结算单元(单元级属性): 该单元在所属月范围内绝大多数人缴五险一金才作为人员归属
        # (与 tax_merge.MAIN_UNIT_MIN_PEOPLE/MAIN_UNIT_INSURED_RATIO 同阈值, Oracle 只读聚合)
        try:
            from queries import get_unit_insurance_stats
            from tax_merge import MAIN_UNIT_MIN_PEOPLE, MAIN_UNIT_INSURED_RATIO
            _units = sorted({int(r.结算单元 or 0) for r in raw_records if r.结算单元})
            _months = sorted({int(r.工资所属年月 or 0) for r in raw_records if r.工资所属年月})
            _stats = get_unit_insurance_stats(conn, _units, _months)
            main_units = {u for u, s in _stats.items()
                          if s["people"] >= MAIN_UNIT_MIN_PEOPLE
                          and s["insured"] / s["people"] >= MAIN_UNIT_INSURED_RATIO}
        except Exception:
            main_units = set()  # 统计失败不影响生成, 回落首薪单元即可
        for tpl in templates:
            if tpl == "normalSalary":
                if confirmed_combos:
                    top = sorted(confirmed_combos, key=lambda c: c.get("person_count", 0), reverse=True)
                    top_names = [c.get("unit_name", "") for c in top[:2] if c.get("unit_name")]
                    months_in = sorted({c["salary_month"] for c in confirmed_combos})
                    month_range = f"{months_in[0]}-{months_in[-1]}" if len(months_in) > 1 else str(months_in[0])
                    if len(confirmed_combos) == 1:
                        file_title = f"{top_names[0]}-{month_range}-{top[0].get('seq', '')}"
                    else:
                        # 多单位: 用过滤条件(发放月份+经办人+单位数+报税人数)代替单位名列表, 尽量简短
                        handlers = "、".join(dict.fromkeys(
                            str(c.get("handler", "") or "").strip()
                            for c in confirmed_combos if str(c.get("handler", "") or "").strip()))
                        n_units = len(confirmed_combos)
                        n_people = sum(int(c.get("person_count", 0) or 0) for c in confirmed_combos)
                        head = f"{month}{handlers}" if handlers else str(month)
                        file_title = f"{head}{n_units}家单位" + (f"{n_people}人" if n_people else "")
                else:
                    file_title = f"劳务派遣人员工资发放表{month}"
                r = generate_normal_salary(records, file_title, OUTPUT_DIR,
                                           tc93_all=tc93_all, abnormal=abnormal,
                                           abnormal_reasons=abnormal_reasons,
                                           combos=confirmed_combos,
                                            tc93_comments=get_tc93_field_comments(conn),
                                            raw_records=raw_records if merge_by_person else None,
                                            merge_mode="pay_month" if merge_by_pay_month else "month",
                                            main_units=main_units,
                                            annual_avg_wage=annual_avg_wage)
            elif tpl == "laborService":
                lab_records, lab_raw, lab_combos = build_labor_service_records(
                    conn, raw_records, confirmed_combos, month,
                    merge_by_person, merge_by_pay_month, zero_codes, excl_codes)
                lab_combo_set = {(int(c.get("unit", 0) or 0),
                                  int(c.get("salary_month", 0) or 0),
                                  str(c.get("seq", "") or "")) for c in lab_combos}
                lab_salary_months = sorted({int(c.get("salary_month", 0) or 0) for c in lab_combos})
                lab_tc93 = []
                for sm in lab_salary_months:
                    lab_tc93.extend(r for r in get_tc93_all_fields(conn, sm)
                                    if (int(r.get("ATB930") or 0), int(r.get("ATC931") or 0),
                                        str(r.get("ATC937") or "")) in lab_combo_set)
                lab_cert_set = {r.身份证.strip().upper() for r in lab_records if r.身份证}
                if lab_cert_set:
                    lab_tc93 = [r for r in lab_tc93
                                if str(r.get("身份证") or "").strip().upper() in lab_cert_set]
                # 报税结算单元只汇总本表实际报税人员 (业务要求, 非查询条件全部单元)
                agg_combos = {}
                for rec in lab_records:
                    key = (int(rec.结算单元 or 0), int(rec.工资所属年月 or 0),
                           str(rec.当月批次 or ""))
                    agg_combos.setdefault(key, {"person_count": 0, "total_income": 0.0})
                    agg_combos[key]["person_count"] += 1
                    agg_combos[key]["total_income"] += float(rec.工资总额 or 0)
                combo_extra = {(int(c.get("unit", 0) or 0), int(c.get("salary_month", 0) or 0),
                                str(c.get("seq", "") or "")): c for c in lab_combos}
                report_combos = []
                key_order = sorted(agg_combos)
                for key in key_order:
                    unit, sm, seq = key
                    extra = combo_extra.get((unit, sm, seq), {})
                    agg = agg_combos[key]
                    report_combos.append({
                        "unit": unit,
                        "unit_name": extra.get("unit_name", ""),
                        "salary_month": sm,
                        "pay_month": extra.get("pay_month", month),
                        "seq": seq,
                        "person_count": agg["person_count"],
                        "total_income": round(agg["total_income"], 2),
                        "handler": extra.get("handler", ""),
                    })
                r = generate_labor_service(lab_records, f"劳务派遣人员工资发放表{month}", OUTPUT_DIR,
                                           tc93_all=lab_tc93, combos=report_combos,
                                           raw_records=lab_raw,
                                           tc93_comments=get_tc93_field_comments(conn),
                                           merge_mode="pay_month" if merge_by_pay_month else "month",
                                           pay_month=month)
            elif tpl == "annualBonus":
                r = generate_annual_bonus(records, f"劳务派遣人员工资发放表{month}", OUTPUT_DIR)
            elif tpl == "personnelInfo":
                r = generate_personnel_info(personnel, f"劳务派遣人员工资发放表{month}", OUTPUT_DIR)
            else:
                continue
            results.append({
                "name": os.path.basename(r.file_path),
                "type": r.template_type,
                "count": r.record_count,
                "validation_pass": r.validation_pass,
                "validation_fail": r.validation_fail,
                "download_url": f"/api/download/{os.path.basename(r.file_path)}"
            })
        
        return jsonify({
            "files": results,
            "abnormal_count": len(abnormal),
            "tc93_total_count": len(tc93_all),
            "merge_warnings": warnings
        })
    except Exception as e:
        return _log_api_error(e)

@app.route("/api/download/<filename>")
def api_download(filename):
    try:
        filepath = os.path.join(OUTPUT_DIR, filename)
        if not os.path.exists(filepath):
            return jsonify({"error": "文件不存在"}), 404
        return send_file(filepath, as_attachment=True)
    except Exception as e:
        return _log_api_error(e)

@app.route("/api/validate/<int:month>")
def api_validate(month):
    try:
        conn = get_connection()
        records = get_salary_records(conn, month)
        report = validate_salary_records(records)
        return jsonify({
            "total_count": report.total_count,
            "pass_count": report.pass_count,
            "fail_count": report.fail_count,
            "pass_rate": f"{report.pass_count/report.total_count*100:.1f}%" if report.total_count > 0 else "0%",
            "details": report.details[:50]  # 只返回前50条失败记录
        })
    except Exception as e:
        return _log_api_error(e)

@app.route("/tax-return")
def page_tax_return():
    return render_template("tax_return.html")

@app.route("/api/pay-months")
def api_pay_months():
    try:
        conn = get_connection()
        return jsonify([{"value": m.value, "label": m.label} for m in get_pay_months(conn)])
    except Exception as e:
        return _log_api_error(e)

@app.route("/api/tax-return/import", methods=["POST"])
def api_tax_return_import():
    try:
        from tax_return import init_db as init_return_db, parse_return_file, import_records
        init_return_db()
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "请选择回盘文件"}), 400
        tmp_path = os.path.join(OUTPUT_DIR, "_return_tmp" + os.path.splitext(file.filename)[1])
        file.save(tmp_path)
        try:
            records = parse_return_file(tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        if not records:
            return jsonify({"error": "未解析到有效记录，请检查文件格式"}), 400
        months = sorted({r["month"] for r in records})
        import_records(records)
        return jsonify({"ok": True, "count": len(records), "months": months})
    except Exception as e:
        return _log_api_error(e)

@app.route("/api/tax-return/status")
def api_tax_return_status():
    try:
        from tax_return import summarize
        month = int(request.args.get("month", 0) or 0)
        if not month:
            return jsonify({"error": "请选择月份"}), 400
        conn = get_connection()
        result = summarize(conn, month)
        result.pop("details", None)
        return jsonify(result)
    except Exception as e:
        return _log_api_error(e)

@app.route("/api/tax-return/details")
def api_tax_return_details():
    try:
        from tax_return import compare_month
        month = int(request.args.get("month", 0) or 0)
        status = request.args.get("status", "")
        search = request.args.get("search", "").strip()
        page = max(1, int(request.args.get("page", 1) or 1))
        page_size = min(500, max(1, int(request.args.get("page_size", 50) or 50)))
        if not month:
            return jsonify({"error": "请选择月份"}), 400
        conn = get_connection()
        details, combo_stats = compare_month(conn, month)
        if status:
            details = [d for d in details if d["status"] == status]
        if search:
            details = [d for d in details
                       if search.lower() in d["name"].lower() or search in d["cert_no"]]
        total = len(details)
        start = (page - 1) * page_size
        return jsonify({"total": total, "page": page, "page_size": page_size,
                        "details": details[start:start + page_size]})
    except Exception as e:
        return _log_api_error(e)

@app.route("/api/tax-return/export")
def api_tax_return_export():
    try:
        from openpyxl import Workbook
        from tax_return import compare_month
        month = int(request.args.get("month", 0) or 0)
        if not month:
            return jsonify({"error": "请选择月份"}), 400
        conn = get_connection()
        details, combo_stats = compare_month(conn, month)
        wb = Workbook()
        ws = wb.active
        ws.title = "报税状态统计"
        ws.append(["月份", "姓名", "证件号码", "结算单元", "所属月份-批次", "系统本期收入", "回盘本期收入", "收入差异", "系统预算个税", "回盘个税(应补退)", "个税差值", "状态"])
        for d in details:
            ws.append([month, d["name"], d["cert_no"], d.get("unit_name", ""), d.get("unit_periods", ""),
                       d["sys_income"], d.get("ret_income"), d.get("income_diff"), d["sys_tax"],
                       d.get("ret_tax"), d["diff"], d["status"]])
        ws2 = wb.create_sheet("按结算单元批次")
        ws2.append(["结算单元", "结算单元名称", "所属月份", "批次", "人数", "已报送", "未报送"])
        for c in combo_stats:
            ws2.append([c["unit"], c.get("unit_name", ""), c["month"], c["seq"], c["count"], c["reported"], c["unreported"]])
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        filename = f"报税状态统计_{month}_{timestamp}.xlsx"
        wb.save(os.path.join(OUTPUT_DIR, filename))
        return jsonify({"download_url": f"/api/download/{filename}"})
    except Exception as e:
        return _log_api_error(e)

@app.route("/tax-adjust")
def page_tax_adjust():
    return render_template("tax_adjust.html")

def _parse_month_arg(raw):
    """解析月份参数，返回 int 或 None（非法时返回 None）。"""
    try:
        month = int(str(raw or "").strip() or 0)
    except (ValueError, TypeError):
        return None
    if not (1000 <= month <= 999999):
        return None
    return month

@app.route("/api/tax-adjust/compare")
def api_tax_adjust_compare():
    try:
        from tax_adjust import compare_tax
        month = _parse_month_arg(request.args.get("month", ""))
        if month is None:
            return jsonify({"error": "月份格式不正确"}), 400
        conn = get_connection()
        return jsonify(compare_tax(conn, month))
    except Exception as e:
        return _log_api_error(e)

@app.route("/api/tax-adjust/export")
def api_tax_adjust_export():
    try:
        from openpyxl import Workbook
        from tax_adjust import compare_tax
        month = _parse_month_arg(request.args.get("month", ""))
        if month is None:
            return jsonify({"error": "月份格式不正确"}), 400
        conn = get_connection()
        result = compare_tax(conn, month)
        wb = Workbook()
        ws = wb.active
        ws.title = "多退少补明细"
        ws.append(["月份", "姓名", "证件号码", "结算单元", "所属月份-批次", "系统预算个税", "回盘个税", "差额", "状态", "建议"])
        for d in result.get("details", []):
            ws.append([month, d.get("name", ""), d.get("cert_no", ""), d.get("unit_name", ""),
                       d.get("unit_periods", ""), d.get("sys_tax") or "", d.get("ret_tax") or "",
                       d.get("diff") or "", d.get("status", ""), d.get("advice", "")])
        ws2 = wb.create_sheet("结算单元汇总")
        ws2.append(["结算单元", "结算单元名称", "人数", "需退总额", "需补总额", "净额"])
        for u in result.get("units", []):
            ws2.append([u.get("unit", ""), u.get("unit_name", ""), u.get("count", 0),
                        u.get("refund", 0), u.get("collect", 0), u.get("net", 0)])
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        filename = f"多退少补建议_{month}_{timestamp}.xlsx"
        wb.save(os.path.join(OUTPUT_DIR, filename))
        return jsonify({"download_url": f"/api/download/{filename}"})
    except Exception as e:
        return _log_api_error(e)

@app.route("/filing-history")
def page_filing_history():
    return render_template("filing_history.html")

@app.route("/api/filing/import", methods=["POST"])
def api_filing_import():
    try:
        from filing_history import init_db as init_filing_db, parse_filing_file, import_filing_records
        init_filing_db()
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "请选择申报文件"}), 400
        results = []
        total = 0
        for i, f in enumerate(files):
            if not f or not f.filename:
                continue
            tmp_path = os.path.join(OUTPUT_DIR, f"_filing_tmp_{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{i}{os.path.splitext(f.filename)[1]}")
            f.save(tmp_path)
            try:
                records = parse_filing_file(tmp_path)
                import_filing_records(records)
                months = sorted({r["month"] for r in records})
                item_type = records[0]["item_type"] if records else ""
                results.append({"filename": f.filename, "count": len(records),
                                "item_type": item_type, "months": months, "error": None})
                total += len(records)
            except Exception as e:
                logging.getLogger(__name__).exception("filing import file error: %s", f.filename)
                results.append({"filename": f.filename, "count": 0,
                                "item_type": "", "months": [], "error": str(e)})
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        return jsonify({"ok": True, "files": results, "total": total})
    except Exception as e:
        return _log_api_error(e)

@app.route("/api/filing/summary")
def api_filing_summary():
    try:
        from filing_history import get_filing_summary
        data = get_filing_summary()
        if isinstance(data, dict) and "groups" in data:
            return jsonify(data)
        return jsonify({"groups": data})
    except Exception as e:
        return _log_api_error(e)

@app.route("/api/filing/records")
def api_filing_records():
    try:
        from filing_history import get_filing_records
        month = request.args.get("month", "").strip()
        if month:
            try:
                month = int(month)
            except ValueError:
                return jsonify({"error": "月份格式不正确"}), 400
            if not (1000 <= month <= 999999):
                return jsonify({"error": "月份格式不正确"}), 400
        else:
            month = None
        item_type = request.args.get("item_type", "").strip() or None
        search = request.args.get("search", "").strip() or None
        page = max(1, int(request.args.get("page", 1) or 1))
        page_size = min(500, max(1, int(request.args.get("page_size", 50) or 50)))
        return jsonify(get_filing_records(month, item_type, search, page, page_size))
    except Exception as e:
        return _log_api_error(e)

@app.route("/personnel-compare")
def page_personnel_compare():
    return render_template("personnel_compare.html")


@app.route("/payroll-records")
def page_payroll_records():
    return render_template("payroll_records.html")


@app.route("/special-units")
def page_special_units():
    return render_template("special_units.html")


@app.route("/api/special-units", methods=["GET"])
def api_special_units_list():
    """获取特殊结算单元配置列表 (SQLite config_db)。"""
    try:
        from config_db import get_special_units
        return jsonify({"units": get_special_units()})
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/special-units", methods=["POST"])
def api_special_units_add():
    """新增特殊结算单元配置 (exclude_all=完全排除不增员不报税, pay_pattern=发薪模式, SQLite config_db)。"""
    try:
        from config_db import add_special_unit
        data = request.get_json()
        unit_code = int(data.get("unit_code", 0) or 0)
        unit_name = str(data.get("unit_name", "") or "")
        exclude_all = bool(data.get("exclude_all", False))
        salary_month_scope = str(data.get("salary_month_scope", "") or "all")
        pay_pattern = str(data.get("pay_pattern", "") or "normal") or "normal"
        if not unit_code:
            return jsonify({"error": "请填写结算单元代码"}), 400
        add_special_unit(unit_code, unit_name, exclude_all=exclude_all,
                         salary_month_scope=salary_month_scope,
                         pay_pattern=pay_pattern)
        return jsonify({"ok": True})
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/special-units/<int:unit_code>/mode", methods=["POST"])
def api_special_units_mode(unit_code):
    """更新特殊结算单元配置的排除模式 (zero_salary_no_add / exclude_all / salary_month_scope / pay_pattern, SQLite)。"""
    try:
        from config_db import update_special_unit
        data = request.get_json() or {}
        exclude_all = data.get("exclude_all")
        zero_salary_no_add = data.get("zero_salary_no_add")
        salary_month_scope = data.get("salary_month_scope")
        pay_pattern = data.get("pay_pattern")
        update_special_unit(unit_code,
                            exclude_all=exclude_all if exclude_all is not None else None,
                            zero_salary_no_add=zero_salary_no_add if zero_salary_no_add is not None else None,
                            salary_month_scope=salary_month_scope if salary_month_scope is not None else None,
                            pay_pattern=pay_pattern if pay_pattern is not None else None)
        return jsonify({"ok": True})
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/special-units/template")
def api_special_units_template():
    """下载特殊结算单元配置导入模板 (6列: 代码/名称/工资为0/完全排除/所属月取数范围/发薪模式)。"""
    try:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "特殊结算单元配置"
        ws.append(["结算单元代码", "结算单元名称", "工资为0不增员不报税", "完全排除不增员不报税",
                   "工资所属月取数范围", "发薪模式"])
        ws.append([None, "测试A-仅工资0", 1, 0, "all", "normal"])
        ws.append([None, "测试B-仅完全排除", 0, 1, "all", "normal"])
        ws.append([None, "测试C-仅最早1个月(压月)", 0, 0, "first_1", "pay_lag"])
        ws.append([None, "测试D-两种+最早2个月", 1, 1, "first_2", "bimonthly"])
        from datetime import datetime as _dt
        filename = f"特殊结算单元配置导入模板_{_dt.now().strftime('%Y%m%d%H%M%S')}.xlsx"
        wb.save(os.path.join(OUTPUT_DIR, filename))
        return jsonify({"download_url": f"/api/download/{filename}"})
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/special-units/unit-list-export")
def api_special_units_unit_list_export():
    """导出结算单元代码-名称对照表 (供填写导入配置用)。"""
    try:
        from openpyxl import Workbook
        from queries import get_units
        conn = get_connection()
        units = get_units(conn)  # 全部结算单元
        wb = Workbook()
        ws = wb.active
        ws.title = "结算单元对照表"
        ws.append(["结算单元代码", "结算单元名称"])
        for u in units:
            ws.append([u["code"], u["name"]])
        from datetime import datetime as _dt
        filename = f"结算单元对照表_{_dt.now().strftime('%Y%m%d%H%M%S')}.xlsx"
        wb.save(os.path.join(OUTPUT_DIR, filename))
        return jsonify({"download_url": f"/api/download/{filename}", "count": len(units)})
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/special-units/export")
def api_special_units_export():
    """导出特殊结算单元配置为 Excel (SQLite config_db)。"""
    try:
        from openpyxl import Workbook
        from config_db import get_special_units
        units = get_special_units()
        wb = Workbook()
        ws = wb.active
        ws.title = "特殊结算单元配置"
        ws.append(["结算单元代码", "结算单元名称", "工资为0不增员不报税", "完全排除不增员不报税",
                   "工资所属月取数范围", "发薪模式"])
        for u in units:
            ws.append([u["code"], u["name"], u["zero_salary_no_add"], u["exclude_all"],
                       u.get("salary_month_scope", "all"), u.get("pay_pattern", "normal")])
        from datetime import datetime as _dt
        filename = f"特殊结算单元配置_{_dt.now().strftime('%Y%m%d%H%M%S')}.xlsx"
        wb.save(os.path.join(OUTPUT_DIR, filename))
        return jsonify({"download_url": f"/api/download/{filename}"})
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/special-units/import", methods=["POST"])
def api_special_units_import():
    """从 Excel 导入特殊结算单元配置 (合并式/更新式)。

    仅新增或更新文件中出现的单元, 绝不删除文件中未出现的单元
    (删除请用逐行删除按钮或 DELETE 接口, 避免误清空例外规则)。
    结算单元代码可留空, 按结算单元名称自动匹配; 名称匹配不到或多个时跳过并提示。
    """
    try:
        from openpyxl import load_workbook
        from config_db import upsert_special_unit_full
        from queries import lookup_unit_codes_by_name
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "请上传配置文件"}), 400
        tmp_path = os.path.join(OUTPUT_DIR, "_special_units_tmp.xlsx")
        file.save(tmp_path)
        conn = get_connection()
        try:
            wb = load_workbook(tmp_path, data_only=True)
            ws = wb.active
            units = []
            skipped = []
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row or all(v is None for v in row):
                    continue
                # 兼容两种格式:
                # 5/6列: 结算单元代码 | 结算单元名称 | 工资为0不增员不报税 | 完全排除不增员不报税
                #        [| 工资所属月取数范围 [| 发薪模式]]
                # 3列: 配置(名称或代码) | 工资为0不增员 | 完全排除不增员
                vals = list(row)
                while len(vals) < 6:
                    vals.append(None)
                if vals[2] is not None or vals[3] is not None:
                    code_raw = vals[0]
                    name = str(vals[1] or "")
                    zero_flag = int(vals[2] or 0)
                    exclude_all = int(vals[3] or 0)
                    scope = str(vals[4] or "").strip() or "all"
                    pattern = str(vals[5] or "").strip() or "normal"
                    code = int(code_raw) if str(code_raw or "").strip().isdigit() else None
                else:
                    code_raw = vals[0]
                    name = str(code_raw or "")
                    zero_flag = int(vals[1] or 0)
                    exclude_all = int(vals[2] or 0)
                    scope = str(vals[3] or "").strip() or "all"
                    pattern = str(vals[4] or "").strip() or "normal"
                    code = int(code_raw) if str(code_raw or "").strip().isdigit() else None
                if scope not in ("all", "first_1", "first_2", "latest_1", "latest_2"):
                    scope = "all"
                if pattern not in ("normal", "pay_lag", "bimonthly"):
                    pattern = "normal"
                if not code:
                    # 代码留空时按名称自动匹配
                    codes = lookup_unit_codes_by_name(conn, name)
                    if len(codes) == 1:
                        code = codes[0]
                    else:
                        skipped.append(f"{name}(匹配到{len(codes)}个代码)")
                        continue
                units.append({"code": code, "name": name,
                              "zero_salary_no_add": zero_flag, "exclude_all": exclude_all,
                              "salary_month_scope": scope, "pay_pattern": pattern})
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        for u in units:
            upsert_special_unit_full(u["code"], u["name"],
                                     zero_salary_no_add=u["zero_salary_no_add"],
                                     exclude_all=u["exclude_all"],
                                     salary_month_scope=u.get("salary_month_scope", "all"),
                                     pay_pattern=u.get("pay_pattern", "normal"))
        resp = {"ok": True, "count": len(units)}
        if skipped:
            resp["skipped"] = skipped
        return jsonify(resp)
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/special-units/<int:unit_code>", methods=["DELETE"])
def api_special_units_delete(unit_code):
    """删除特殊结算单元配置 (SQLite)。"""
    try:
        from config_db import delete_special_unit
        delete_special_unit(unit_code)
        return jsonify({"ok": True})
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/payroll-records")
def api_payroll_records():
    """查询工资发放情况 (TC8M, 按经办年月范围, 已发放)。"""
    try:
        from queries import get_payroll_records
        start = int(request.args.get("start", 0) or 0)
        end = int(request.args.get("end", 0) or 0)
        if not start or not end:
            return jsonify({"error": "请选择发薪月份(起始)和(结束)"}), 400
        if end < start:
            return jsonify({"error": "发薪月份(结束)不能早于(起始)"}), 400
        conn = get_connection()
        records = get_payroll_records(conn, start, end)
        return jsonify({"records": records, "count": len(records)})
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/personnel-compare/filters")
def api_personnel_compare_filters():
    """查询经办人/结算单元/单位列表 (供筛选下拉框)。

    经办人分三来源: 发薪经办人(TC8M) / 做工资经办人(TC93) / 合同经办人(TC90),
    三来源可能不同人 (2026 年存在不一致批次), 界面单列供用户分别选择。
    """
    try:
        from queries import get_handler_options, get_units, get_depts
        conn = get_connection()
        pay_month = int(request.args.get("pay_month", 0) or 0)
        options = get_handler_options(conn, pay_month)
        options.update({
            "units": get_units(conn, pay_month),
            "depts": get_depts(conn, pay_month),
        })
        return jsonify(options)
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/personnel-compare/default-report-month")
def api_personnel_compare_default_month():
    """查询默认上报发薪月份 (1-15日上月, 16-月末本月)。"""
    try:
        from queries import get_default_report_month
        conn = get_connection()
        return jsonify({"month": get_default_report_month(conn)})
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/personnel-compare/latest-pay-date")
def api_personnel_compare_latest_pay_date():
    """查询最近一笔工资发放日期 (TC8M.AAE036)。"""
    try:
        from queries import get_latest_pay_date
        conn = get_connection()
        pay_month, pay_date = get_latest_pay_date(conn)
        return jsonify({
            "pay_month": pay_month,
            "pay_date": pay_date.strftime("%Y-%m-%d") if pay_date else None,
        })
    except Exception as e:
        return _log_api_error(e)


@app.route("/api/personnel-compare/compare", methods=["POST"])
def api_personnel_compare():
    """上传个税端导出文件 + 选择最近1~2次发薪月份 → 增减员比对 → 生成 Excel。

    离职时间截止日期 (termination_deadline, YYYY-MM-DD) 用于过滤 TC90 离职日期:
    超过截止日期的合同终止日期视为未到期, 归入待确认。

    参数:
    - file: 个税端导出文件 (.xls)
    - pay_month_start/pay_month_end: 发薪月份时间段 (显式起止)
    - unpaid_month_start/unpaid_month_end: 未发薪工资表所属月份时间段 (显式起止)
    - pay_start_time/pay_end_time: 可选发薪经办时间 (精确到时分秒, 默认关闭)
    - contract_start_time/contract_end_time: 合同签署时间范围 (精确到时分秒, 可选)
    - termination_deadline: 离职时间截止日期 (默认最近发薪日期)
    """
    try:
        from queries import (get_payroll_cert_numbers, get_tc90_salary_end_dates,
                             get_payroll_personnel, get_unpaid_salary_persons,
                             get_pay_month_range, get_unpaid_month_range,
                             get_contract_signed_persons, get_contract_date_range,
                             get_latest_pay_date, get_personnel_by_certs)
        from templates_gen.personnel_compare import compare_personnel, generate_compare_excel
        from templates_gen.tax_export_parser import parse_tax_export

        file = request.files.get("file")
        if not file:
            return jsonify({"error": "请上传个税端导出文件"}), 400
        pay_month_start = int(request.form.get("pay_month_start", 0) or 0)
        if not pay_month_start:
            return jsonify({"error": "请选择发薪月份(起始)"}), 400
        pay_month_end = int(request.form.get("pay_month_end", 0) or pay_month_start)
        if pay_month_end < pay_month_start:
            return jsonify({"error": "发薪月份(结束)不能早于(起始)"}), 400
        unpaid_month_start = int(request.form.get("unpaid_month_start", 0) or 0)
        unpaid_month_end = int(request.form.get("unpaid_month_end", 0) or unpaid_month_start)
        if unpaid_month_start and unpaid_month_end < unpaid_month_start:
            return jsonify({"error": "未发薪所属月份(结束)不能早于(起始)"}), 400
        contract_start_time = request.form.get("contract_start_time", "").strip()
        contract_end_time = request.form.get("contract_end_time", "").strip()
        from datetime import datetime as _dt
        contract_start_dt = _dt.strptime(contract_start_time, "%Y-%m-%dT%H:%M") if contract_start_time else None
        contract_end_dt = _dt.strptime(contract_end_time, "%Y-%m-%dT%H:%M") if contract_end_time else None
        # 可选经办时间过滤 (精确到时分秒, 默认关闭)
        pay_start_time = request.form.get("pay_start_time", "").strip()
        pay_end_time = request.form.get("pay_end_time", "").strip()
        from datetime import datetime as _dt
        pay_start_dt = _dt.strptime(pay_start_time, "%Y-%m-%dT%H:%M") if pay_start_time else None
        pay_end_dt = _dt.strptime(pay_end_time, "%Y-%m-%dT%H:%M") if pay_end_time else None
        filter_handlers = [h for h in request.form.getlist("handler") if h.strip()]
        filter_salary_handlers = [h for h in request.form.getlist("salary_handler") if h.strip()]
        filter_contract_handlers = [h for h in request.form.getlist("contract_handler") if h.strip()]
        # 三来源合并去重 (去重保序), 匹配规则: 任一来源命中即通过
        filter_handlers = list(dict.fromkeys(
            filter_handlers + filter_salary_handlers + filter_contract_handlers))
        filter_units = [int(u) for u in request.form.getlist("unit") if u.strip()]
        filter_depts = [d for d in request.form.getlist("dept") if d.strip()]
        deadline = request.form.get("termination_deadline", "").strip()
        deadline_date = None
        if deadline:
            try:
                deadline_date = _dt.strptime(deadline, "%Y-%m-%d").date()
            except ValueError:
                return jsonify({"error": "离职时间截止日期格式错误 (应为 YYYY-MM-DD)"}), 400
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext != ".xls":
            return jsonify({"error": "仅支持 .xls 格式的个税端导出文件"}), 400

        tmp_path = os.path.join(OUTPUT_DIR, "_compare_tmp" + ext)
        file.save(tmp_path)
        try:
            tax_export_persons = parse_tax_export(tmp_path)
        except Exception as e:
            return jsonify({"error": f"解析个税端导出文件失败: {e}"}), 400
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        if not tax_export_persons:
            return jsonify({"error": "导出文件中未解析到有效人员记录"}), 400

        conn = get_connection()
        # 发薪月份范围: 用户显式选择 [起始, 结束]
        pay_months = list(range(pay_month_start, pay_month_end + 1))
        payroll_certs = set()
        payroll_start_certs = set()
        payroll_personnel = []
        seen = set()
        for m in pay_months:
            payroll_certs |= get_payroll_cert_numbers(conn, m, start_time=pay_start_dt, end_time=pay_end_dt)
            if m == pay_month_start:
                payroll_start_certs |= get_payroll_cert_numbers(conn, m, start_time=pay_start_dt, end_time=pay_end_dt)
            for p in get_payroll_personnel(conn, m, start_time=pay_start_dt, end_time=pay_end_dt):
                if p.身份证 and p.身份证 not in seen:
                    seen.add(p.身份证)
                    payroll_personnel.append(p)
        # 未发薪工资表: 用户显式选择 [起始, 结束]
        unpaid_persons = set()
        unpaid_months = []
        if unpaid_month_start:
            unpaid_months = list(range(unpaid_month_start, unpaid_month_end + 1))
            unpaid_persons = get_unpaid_salary_persons(conn, unpaid_months)
        # 合同签署: 合同开始日期在起始~结束时间范围内 (精确到时分秒)
        contract_persons = set()
        if contract_start_dt or contract_end_dt:
            contract_persons = get_contract_signed_persons(conn, contract_start_dt, contract_end_dt)
        # 疑似近期离职: 个税端未标记离职(无离职日期)且不在发薪/未发薪/合同名单
        active_certs = {
            str(p.get("证件号码") or "").strip().upper()
            for p in tax_export_persons
            if not str(p.get("离职日期") or "").strip()
        }
        protected = payroll_certs | unpaid_persons | contract_persons
        suspect_certs = active_certs - protected
        # 压月发薪延期规则: 最后一笔工资未发放的人员不能减员 (自动判定, 不依赖未发薪月份选择)
        from queries import get_latest_unpaid_persons
        unpaid_latest = get_latest_unpaid_persons(conn, suspect_certs)
        suspect_certs -= unpaid_latest
        salary_end_dates = get_tc90_salary_end_dates(conn, suspect_certs)
        # 离职日期超过截止日期的视为合同未到期, 不列为已离职
        if deadline_date:
            salary_end_dates = {
                c: d for c, d in salary_end_dates.items()
                if d.date() <= deadline_date
            }
        # 延期发放人员: 申报月无发放但次月月初(1~10日)已发 (在职证据) → 零申报而非待确认
        from queries import get_deferred_pay_persons
        deferred_pay = get_deferred_pay_persons(conn, pay_months)
        add_rows, departed_rows, pending_rows, stats = compare_personnel(
            tax_export_persons, payroll_certs, payroll_personnel, salary_end_dates,
            unpaid_persons=unpaid_persons, contract_signed_persons=contract_persons,
            unpaid_latest_persons=unpaid_latest, payroll_start_certs=payroll_start_certs,
            deferred_pay_persons=deferred_pay)
        # 特殊结算单元: 工资为0不增员不报税 + 完全排除不增员不报税
        # 配置存 SQLite (config_db), Oracle 只读
        from config_db import get_zero_salary_unit_codes, get_excluded_unit_codes
        from queries import get_zero_salary_certs, get_excluded_unit_certs, get_person_units
        exclude_certs = set()
        zero_codes = get_zero_salary_unit_codes()
        excl_codes = get_excluded_unit_codes()
        if zero_codes:
            exclude_certs |= get_zero_salary_certs(conn, pay_months, unpaid_months, zero_codes)
        if excl_codes:
            relevant_months = sorted(set(pay_months) | set(unpaid_months))
            exclude_certs |= get_excluded_unit_certs(conn, pay_months, excl_codes,
                                                     relevant_months=relevant_months)
        # 经办人/结算单元/单位过滤 + 备注(结算单元名称)数据
        tax_cert_set = {str(p.get("证件号码") or "").strip().upper()
                        for p in tax_export_persons}
        add_candidates = (payroll_certs | unpaid_persons | contract_persons) - tax_cert_set
        # 减员候选需要人员归属信息 (个税端未标记离职人员: 过滤 + 备注都需要)
        unit_query_certs = set(add_candidates)
        if filter_handlers or filter_units or filter_depts:
            active_certs = {str(p.get("证件号码") or "").strip().upper()
                            for p in tax_export_persons
                            if not str(p.get("离职日期") or "").strip()}
            unit_query_certs |= active_certs
            unit_query_certs |= payroll_certs
        else:
            # 无过滤时仅需减员候选的归属 (备注=结算单元名称)
            unit_query_certs |= suspect_certs
        person_units = get_person_units(conn, unit_query_certs, pay_months, unpaid_months)
        # 合并最后一份合同信息 (合同经办人 + 无工资记录人员补充单位名称)
        from queries import get_person_units_contract, get_last_pay_handlers, get_last_salary_handlers
        for cert, info in get_person_units_contract(conn, unit_query_certs).items():
            base = person_units.setdefault(cert, {})
            base["contract_handlers"] = info.get("contract_handlers") or []
            if not base.get("unit_name"):
                base["unit_code"] = info.get("unit_code") or 0
                base["unit_name"] = info.get("unit_name") or ""
                base["dept_name"] = info.get("dept_name") or ""
        # 减员人员经办人来源: 最后一次发薪 (TC8M) / 最后一次做工资 (TC93), 链式回落
        for cert, hs in get_last_pay_handlers(conn, unit_query_certs).items():
            person_units.setdefault(cert, {})["last_pay_handlers"] = hs
        for cert, hs in get_last_salary_handlers(conn, unit_query_certs).items():
            person_units.setdefault(cert, {})["last_salary_handlers"] = hs
        if exclude_certs or filter_handlers or filter_units or filter_depts:
            add_rows, departed_rows, pending_rows, stats = compare_personnel(
                tax_export_persons, payroll_certs, payroll_personnel, salary_end_dates,
                unpaid_persons=unpaid_persons, contract_signed_persons=contract_persons,
                person_units=person_units, filter_handlers=filter_handlers,
                filter_units=filter_units, filter_depts=filter_depts,
                exclude_certs=exclude_certs, unpaid_latest_persons=unpaid_latest,
                payroll_start_certs=payroll_start_certs,
                deferred_pay_persons=deferred_pay)
        # 补充增员人员详细信息 (未发薪/合同签署人员不在 payroll_personnel 中)
        if stats["add_count"] > len(add_rows):
            from templates_gen.personnel_compare import IDX_证件号码, map_personnel_info_to_row
            have_certs = {r[IDX_证件号码] for r in add_rows}
            need_certs = stats["add_certs"] - have_certs
            if need_certs:
                extra_people = get_personnel_by_certs(conn, need_certs)
                for p in extra_people:
                    row = map_personnel_info_to_row(p)
                    info = (person_units or {}).get(str(p.身份证 or "").strip().upper())
                    if info:
                        # 备注优先结算单元名称(ATB931); 降级单位名称时标明"非结算单元名称"
                        remark = info.get("unit_name") or ""
                        if remark:
                            row[25] = remark
                        elif info.get("dept_name"):
                            row[25] = f"单位名称（非结算单元名称）：{info['dept_name']}"
                    add_rows.append(row)
        # 增员名单/验证按备注(结算单元)排序, 相同结算单元相邻
        add_rows.sort(key=lambda r: str(r[25] or ""))
        # 增员验证 Sheet: 为每个增员人员组装验证行 + TC93/TC8M/TC90 明细
        from queries import get_salary_details, get_tc8m_records, get_tc90_records
        from templates_gen.personnel_compare import (IDX_证件号码, build_verify_row,
                                                     map_personnel_info_to_row as _map_row)
        # 减员名单备注 = 结算单元名称 (与增员名单一致), 并按结算单元排序
        for row in departed_rows + pending_rows:
            info = person_units.get(str(row[IDX_证件号码] or "").strip().upper())
            if info:
                remark = info.get("unit_name") or ""
                if remark:
                    row[25] = remark
                elif info.get("dept_name"):
                    row[25] = f"单位名称（非结算单元名称）：{info['dept_name']}"
        departed_rows.sort(key=lambda r: str(r[25] or ""))
        pending_rows.sort(key=lambda r: str(r[25] or ""))
        add_certs_final = {r[IDX_证件号码] for r in add_rows}
        verify_params = {
            "pay_months": pay_months,
            "unpaid_months": unpaid_months,
            "contract_start": contract_start_dt.strftime("%Y-%m-%d %H:%M") if contract_start_dt else "",
            "contract_end": contract_end_dt.strftime("%Y-%m-%d %H:%M") if contract_end_dt else "",
        }
        salary_details = get_salary_details(conn, add_certs_final,
                                            sorted(set(pay_months + unpaid_months)))
        tc8m_details = get_tc8m_records(conn, add_certs_final, pay_months)
        tc90_details = get_tc90_records(conn, add_certs_final)
        # 发薪工资明细: 按 TC8M 发薪记录的所属月份范围查询 (可能早于发薪月份)
        paid_salary_months = sorted({r["salary_month"] for r in tc8m_details
                                     if r.get("salary_month")})
        paid_salary_details = get_salary_details(conn, add_certs_final, paid_salary_months) if paid_salary_months else []
        # 合同开始日期: 取 TC90 最早的 ATC90C
        contract_start_map = {}
        for r in tc90_details:
            cert = r["cert"]
            if cert not in contract_start_map or (r["合同开始日期"] and r["合同开始日期"] < contract_start_map[cert]):
                contract_start_map[cert] = r["合同开始日期"]
        salary_by_cert = {}
        for r in salary_details:
            salary_by_cert.setdefault(r["cert"], []).append(r)
        paid_salary_by_cert = {}
        for r in paid_salary_details:
            paid_salary_by_cert.setdefault(r["cert"], []).append(r)
        tc8m_by_cert = {}
        for r in tc8m_details:
            tc8m_by_cert.setdefault(r["cert"], []).append(r)
        tc90_by_cert = {}
        for r in tc90_details:
            tc90_by_cert.setdefault(r["cert"], []).append(r)
        verify_rows = []
        for r in add_rows:
            cert = r[IDX_证件号码]
            verify_rows.append(build_verify_row(
                r, cert, verify_params,
                paid_salary_by_cert.get(cert, []),
                salary_by_cert.get(cert, []),
                tc8m_by_cert.get(cert, []),
                contract_start_map.get(cert),
                contract_details=tc90_by_cert.get(cert, [])))
        # 减员验证 Sheet: 为近期离职/待确认离职人员组装验证行
        from templates_gen.personnel_compare import build_remove_verify_row
        from queries import get_last_pay_records
        remove_all = [(r, "近期离职") for r in departed_rows] + [(r, "待确认近期离职") for r in pending_rows]
        remove_certs = {r[IDX_证件号码] for r, _ in remove_all}
        remove_verify_rows = []
        if remove_certs:
            remove_salary = get_salary_details(conn, remove_certs,
                                               sorted(set(pay_months + unpaid_months)))
            remove_tc8m = get_tc8m_records(conn, remove_certs, pay_months)
            remove_tc90 = get_tc90_records(conn, remove_certs)
            remove_last_pay = get_last_pay_records(conn, remove_certs)
            remove_salary_by_cert = {}
            for r in remove_salary:
                remove_salary_by_cert.setdefault(r["cert"], []).append(r)
            remove_tc8m_by_cert = {}
            for r in remove_tc8m:
                remove_tc8m_by_cert.setdefault(r["cert"], []).append(r)
            remove_tc90_by_cert = {}
            for r in remove_tc90:
                remove_tc90_by_cert.setdefault(r["cert"], []).append(r)
            tax_person_by_cert = {}
            for p in tax_export_persons:
                pc = str(p.get("证件号码") or "").strip().upper()
                if pc:
                    tax_person_by_cert.setdefault(pc, p)
            remove_contract_start = {}
            remove_contract_end = {}
            remove_contract_end_d90 = {}
            for r in remove_tc90:
                c = r["cert"]
                if c not in remove_contract_start or (r["合同开始日期"] and r["合同开始日期"] < remove_contract_start[c]):
                    remove_contract_start[c] = r["合同开始日期"]
                if r.get("合同终止日期"):
                    if c not in remove_contract_end_d90 or r["合同终止日期"] > remove_contract_end_d90[c]:
                        remove_contract_end_d90[c] = r["合同终止日期"]
            # 离职日期: 用已按截止日期过滤的 salary_end_dates (判定口径, ATC90AV月末)
            # 待确认人员 (无有效工资结束年月) 离职日期为空
            for c, dt in salary_end_dates.items():
                if c in remove_contract_end:
                    remove_contract_end[c] = max(remove_contract_end[c], dt)
                else:
                    remove_contract_end[c] = dt
            for row, rtype in remove_all:
                cert = row[IDX_证件号码]
                remove_verify_rows.append(build_remove_verify_row(
                    row, cert, rtype, verify_params,
                    [],  # 减员无发薪记录
                    remove_salary_by_cert.get(cert, []),
                    remove_tc8m_by_cert.get(cert, []),
                    remove_contract_start.get(cert),
                    remove_contract_end.get(cert),
                    contract_end_d90=remove_contract_end_d90.get(cert),
                    contract_details=remove_tc90_by_cert.get(cert, []),
                    tax_person=tax_person_by_cert.get(cert, {}),
                    last_pay=remove_last_pay.get(cert)))
        tc93_all = []
        seen_tc93 = set()
        for r in paid_salary_details + salary_details:
            key = (r["cert"], r["salary_month"], r["seq"])
            if key in seen_tc93:
                continue
            seen_tc93.add(key)
            tc93_all.append(r)
        tc93_rows = [[r["cert"], r["姓名"], r["unit_code"], r["unit_name"], r["salary_month"], r["seq"],
                      r["应发工资"], r["本期收入"], r["养老"], r["医疗"], r["失业"], r["公积金"]]
                     for r in tc93_all]
        tc8m_all = []
        seen_tc8m = set()
        for r in tc8m_details:
            key = (r["cert"], r["salary_month"], r["seq"])
            if key in seen_tc8m:
                continue
            seen_tc8m.add(key)
            tc8m_all.append(r)
        tc8m_rows = [[r["cert"], r["姓名"], r["unit_code"], r["unit_name"], r["pay_month"],
                      r["salary_month"], r["seq"], r["handler"]] for r in tc8m_all]
        # TC90 每人最多 2 条 (按合同开始日期倒序取最近)
        tc90_by_cert = {}
        for r in tc90_details:
            tc90_by_cert.setdefault(r["cert"], []).append(r)
        tc90_rows = []
        for cert, recs in tc90_by_cert.items():
            recs_sorted = sorted(recs, key=lambda x: x["合同开始日期"], reverse=True)
            for r in recs_sorted[:2]:
                tc90_rows.append([r["cert"], r["姓名"], r["unit_code"], r["unit_name"],
                                  r["合同开始日期"], r["合同终止日期"], r["单位名称"], r["经办人"]])
        # 零申报人群: 个税端在职且本期无工资、未减员, 排除特殊结算单元。
        # 主"零申报" sheet 排除待确认; 待确认另立独立"待确认零申报" sheet。
        # 组装 30 列零申报行 (本期收入+养老/医疗/失业/公积金填0, 其余数值列留空), 备注=结算单元名称。
        from templates_gen.personnel_compare import build_zero_declare_row
        zero_person_by_cert = {str(p.get("证件号码") or "").strip().upper(): p
                               for p in tax_export_persons}
        zero_rows = []
        for cert in sorted(stats.get("zero_certs") or set()):
            person = zero_person_by_cert.get(cert) or {"证件号码": cert}
            info = person_units.get(cert)
            if info:
                remark = info.get("unit_name") or ""
                if not remark and info.get("dept_name"):
                    remark = f"单位名称（非结算单元名称）：{info['dept_name']}"
                person = dict(person)
                person["备注"] = remark
            zero_rows.append(build_zero_declare_row(person))
        zero_pending_rows = []
        for cert in sorted(stats.get("zero_pending_certs") or set()):
            person = zero_person_by_cert.get(cert) or {"证件号码": cert}
            info = person_units.get(cert)
            if info:
                remark = info.get("unit_name") or ""
                if not remark and info.get("dept_name"):
                    remark = f"单位名称（非结算单元名称）：{info['dept_name']}"
                person = dict(person)
                person["备注"] = remark
            zero_pending_rows.append(build_zero_declare_row(person))
        month_label = f"{pay_month_start}-{pay_month_end}" if pay_month_end != pay_month_start else str(pay_month_start)
        result = generate_compare_excel(add_rows, departed_rows, pending_rows, stats, OUTPUT_DIR, month_label,
                                        verify_rows=verify_rows, tc93_rows=tc93_rows,
                                        tc8m_rows=tc8m_rows, tc90_rows=tc90_rows,
                                        remove_verify_rows=remove_verify_rows, zero_rows=zero_rows,
                                        zero_pending_rows=zero_pending_rows)
        return jsonify({
            "add_count": stats["add_count"],
            "departed_count": stats["departed_count"],
            "pending_count": stats["pending_count"],
            "tax_total": stats["tax_total"],
            "payroll_total": stats["payroll_total"],
            "zero_count": stats.get("zero_count", 0),
            "zero_pending_count": stats.get("zero_pending_count", 0),
            "active_total": stats.get("active_total", 0),
            "unpaid_total": stats.get("unpaid_total", 0),
            "contract_total": stats.get("contract_total", 0),
            "filtered_active_count": stats.get("filtered_active_count", 0),
            "filtered_payroll_count": stats.get("filtered_payroll_count", 0),
            "file_name": os.path.basename(result.file_path),
            "download_url": f"/api/download/{os.path.basename(result.file_path)}",
        })
    except Exception as e:
        return _log_api_error(e)


init_db()

# 初始化自建 SQLite 配置库 (Oracle 只读, 配置数据不入 Oracle)
import config_db as _config_db
_config_db.init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("FLASK_DEBUG", "0") == "1")