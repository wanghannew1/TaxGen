"""Flask 主应用 - 个税模板填表工具"""
import atexit
import os
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file
from db import init_db, get_connection, close_db
from queries import get_available_months, get_salary_records, get_personnel_info, get_suggestions, search_tc8m, get_abnormal_records, get_tc93_all_fields, get_tc93_field_comments, get_merge_warnings, get_pay_months, get_payroll_cert_numbers, get_tc90_termination_dates, get_payroll_personnel
from templates_gen.normal_salary import generate_normal_salary, generate_tc93_full_sheet, generate_abnormal_sheet
from templates_gen.labor_service import generate_labor_service
from templates_gen.annual_bonus import generate_annual_bonus
from templates_gen.personnel_info import generate_personnel_info
from templates_gen.validation import validate_salary_records

app = Flask(__name__)
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 注意: 连接池在进程生命周期内保持打开 (oracledb 连接在 GC 时自动归还池),
# 仅在进程退出时关闭。不能在 teardown_appcontext 中调用 close_db(),
# 否则每个请求后池被关闭, 后续所有数据库请求都会失败。
atexit.register(close_db)


def merge_records_by_person(records, by_pay_month: bool = False):
    """按人合并多条工资记录为一笔，基准取时间上最后一个批次(批次号最大、流水号最大)。

    by_pay_month=False: 按人+所属月份合并（现状，同人同月多笔合并，同一人可能多行）。
    by_pay_month=True:  按人+发放月份合并（同一发放月份内每人一行，跨所属月份的收入/五险一金/个税全部合计）。
                        仅适用于组合确认流程——所有组合共享同一发放月份(TC8M.ATC8G7)。
    """
    from copy import deepcopy
    group_key = (lambda rec: (rec.职工号,)) if by_pay_month \
        else (lambda rec: (rec.职工号, rec.工资所属年月))
    groups = {}
    for rec in records:
        groups.setdefault(group_key(rec), []).append(rec)

    merged = []
    for key, recs in groups.items():
        if by_pay_month:
            # 跨所属月份合并时"最后批次"语义失效，基准取流水号最大(最新经办)的记录
            base = max(recs, key=lambda r: r.tc930_id)
        else:
            base = max(recs, key=lambda r: (int(r.当月批次 or 0), r.tc930_id))
        m = deepcopy(base)
        for rec in recs:
            if rec is base:
                continue
            m.应发工资 += rec.应发工资
            m.实发工资 += rec.实发工资
            m.个人所得税 += rec.个人所得税
            m.工资总额 += rec.工资总额
            m.独生子女费 += rec.独生子女费
            m.采暖费 += rec.采暖费
            m.奖金 += rec.奖金
            m.养老个人 += rec.养老个人
            m.医疗个人 += rec.医疗个人
            m.失业个人 += rec.失业个人
            m.公积金个人 += rec.公积金个人
            m.补缴及退款保险金额个人 += rec.补缴及退款保险金额个人
            m.大病险个人 += rec.大病险个人
            m.补发3 += rec.补发3
            m.个人交纳现金 += rec.个人交纳现金
            m.个人其他调整 += rec.个人其他调整
            m.个人欠款 += rec.个人欠款
            m.扣款大病险 += rec.扣款大病险
            m.税后工会会费 += rec.税后工会会费
            m.个人代理费 += rec.个人代理费
            m.意外险个人 += rec.意外险个人
        merged.append(m)
    return merged

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
            salary_months = {c["salary_month"] for c in confirmed_combos}
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
        raw_records = records
        if merge_by_person:
            records = merge_records_by_person(records, by_pay_month=merge_by_pay_month)
        warnings = []
        if confirmed_combos and merge_by_person:
            persons = list({r.职工号 for r in raw_records})
            warnings = get_merge_warnings(conn, [month],
                                          combo_set if confirmed_combos else set(), persons)
        abnormal_reasons = {r.get("ATC930"): f"ATC93G={r.get('ATC93G', 'NULL')}(未结算)" for r in abnormal}
        
        results = []
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
                        file_title = f"{'、'.join(top_names)}等{len(confirmed_combos)}个单位{month_range}工资"
                else:
                    file_title = f"劳务派遣人员工资发放表{month}"
                r = generate_normal_salary(records, file_title, OUTPUT_DIR,
                                           tc93_all=tc93_all, abnormal=abnormal,
                                           abnormal_reasons=abnormal_reasons,
                                           combos=confirmed_combos,
                                            tc93_comments=get_tc93_field_comments(conn),
                                            raw_records=raw_records if merge_by_person else None,
                                            merge_mode="pay_month" if merge_by_pay_month else "month")
            elif tpl == "laborService":
                r = generate_labor_service(records, f"劳务派遣人员工资发放表{month}", OUTPUT_DIR)
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
        return jsonify({"error": str(e)}), 500

@app.route("/api/download/<filename>")
def api_download(filename):
    try:
        filepath = os.path.join(OUTPUT_DIR, filename)
        if not os.path.exists(filepath):
            return jsonify({"error": "文件不存在"}), 404
        return send_file(filepath, as_attachment=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

@app.route("/tax-return")
def page_tax_return():
    return render_template("tax_return.html")

@app.route("/api/pay-months")
def api_pay_months():
    try:
        conn = get_connection()
        return jsonify([{"value": m.value, "label": m.label} for m in get_pay_months(conn)])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

@app.route("/personnel-compare")
def page_personnel_compare():
    return render_template("personnel_compare.html")


@app.route("/special-units")
def page_special_units():
    return render_template("special_units.html")


@app.route("/api/special-units", methods=["GET"])
def api_special_units_list():
    """获取特殊结算单元配置列表。"""
    try:
        from queries import get_special_units
        conn = get_connection()
        return jsonify({"units": get_special_units(conn)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/special-units", methods=["POST"])
def api_special_units_add():
    """新增特殊结算单元配置。"""
    try:
        from queries import add_special_unit
        data = request.get_json()
        unit_code = int(data.get("unit_code", 0) or 0)
        unit_name = str(data.get("unit_name", "") or "")
        if not unit_code:
            return jsonify({"error": "请填写结算单元代码"}), 400
        conn = get_connection()
        add_special_unit(conn, unit_code, unit_name)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/special-units/<int:unit_code>", methods=["DELETE"])
def api_special_units_delete(unit_code):
    """删除特殊结算单元配置。"""
    try:
        from queries import delete_special_unit
        conn = get_connection()
        delete_special_unit(conn, unit_code)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/personnel-compare/filters")
def api_personnel_compare_filters():
    """查询经办人/结算单元列表 (供筛选下拉框)。"""
    try:
        from queries import get_handlers, get_units
        conn = get_connection()
        pay_month = int(request.args.get("pay_month", 0) or 0)
        return jsonify({
            "handlers": get_handlers(conn, pay_month),
            "units": get_units(conn, pay_month),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/personnel-compare/default-report-month")
def api_personnel_compare_default_month():
    """查询默认上报发薪月份 (1-15日上月, 16-月末本月)。"""
    try:
        from queries import get_default_report_month
        conn = get_connection()
        return jsonify({"month": get_default_report_month(conn)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


@app.route("/api/personnel-compare/compare", methods=["POST"])
def api_personnel_compare():
    """上传个税端导出文件 + 选择最近1~2次发薪月份 → 增减员比对 → 生成 Excel。

    离职时间截止日期 (termination_deadline, YYYY-MM-DD) 用于过滤 TC90 离职日期:
    超过截止日期的合同终止日期视为未到期, 归入待确认。

    参数:
    - file: 个税端导出文件 (.xls)
    - pay_month: 发薪月份 (选中月份至最近发薪月份自动扩展)
    - unpaid_salary_month: 未发薪工资表所属月份 (选中月份至当前月自动扩展)
    - contract_month: 合同签署时间 (选中月份当月1日至上报月份最后一天)
    - termination_deadline: 离职时间截止日期 (默认最近发薪日期)
    """
    try:
        from queries import (get_payroll_cert_numbers, get_tc90_termination_dates,
                             get_payroll_personnel, get_unpaid_salary_persons,
                             get_pay_month_range, get_unpaid_month_range,
                             get_contract_signed_persons, get_contract_date_range,
                             get_latest_pay_date, get_personnel_by_certs)
        from templates_gen.personnel_compare import compare_personnel, generate_compare_excel
        from templates_gen.tax_export_parser import parse_tax_export

        file = request.files.get("file")
        if not file:
            return jsonify({"error": "请上传个税端导出文件"}), 400
        pay_month = int(request.form.get("pay_month", 0) or 0)
        if not pay_month:
            return jsonify({"error": "请选择发薪月份"}), 400
        unpaid_month = int(request.form.get("unpaid_salary_month", 0) or 0)
        contract_month = int(request.form.get("contract_month", 0) or 0)
        filter_handlers = [h for h in request.form.getlist("handler") if h.strip()]
        filter_units = [int(u) for u in request.form.getlist("unit") if u.strip()]
        deadline = request.form.get("termination_deadline", "").strip()
        deadline_date = None
        if deadline:
            from datetime import datetime as _dt
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
        # 发薪月份范围: 选中月份 → 最近发薪月份
        pay_months = get_pay_month_range(pay_month, conn)
        payroll_certs = set()
        payroll_personnel = []
        seen = set()
        for m in pay_months:
            payroll_certs |= get_payroll_cert_numbers(conn, m)
            for p in get_payroll_personnel(conn, m):
                if p.身份证 and p.身份证 not in seen:
                    seen.add(p.身份证)
                    payroll_personnel.append(p)
        # 未发薪工资表: 选中所属月份 → 当前月
        unpaid_persons = set()
        if unpaid_month:
            unpaid_months = get_unpaid_month_range(unpaid_month)
            unpaid_persons = get_unpaid_salary_persons(conn, unpaid_months)
        # 合同签署: 选中月份当月1日 → 上报月份最后一天
        contract_persons = set()
        if contract_month:
            _, report_pay_date = get_latest_pay_date(conn)
            start_date, end_date = get_contract_date_range(contract_month, pay_month)
            contract_persons = get_contract_signed_persons(conn, start_date, end_date)
        # 疑似近期离职: 个税端未标记离职(无离职日期)且不在发薪/未发薪/合同名单
        active_certs = {
            str(p.get("证件号码") or "").strip().upper()
            for p in tax_export_persons
            if not str(p.get("离职日期") or "").strip()
        }
        protected = payroll_certs | unpaid_persons | contract_persons
        suspect_certs = active_certs - protected
        termination_dates = get_tc90_termination_dates(conn, suspect_certs)
        # 离职日期超过截止日期的视为合同未到期, 不列为已离职
        if deadline_date:
            termination_dates = {
                c: d for c, d in termination_dates.items()
                if d.date() <= deadline_date
            }
        add_rows, departed_rows, pending_rows, stats = compare_personnel(
            tax_export_persons, payroll_certs, payroll_personnel, termination_dates,
            unpaid_persons=unpaid_persons, contract_signed_persons=contract_persons)
        # 特殊结算单元: 工资为0不增员
        from queries import get_special_units, get_zero_salary_certs, get_person_units
        exclude_certs = get_zero_salary_certs(conn, pay_month) if get_special_units(conn) else set()
        # 经办人/结算单元过滤 + 备注(结算单元名称)数据
        person_units = None
        all_certs = (payroll_certs | unpaid_persons | contract_persons |
                     {str(p.get("证件号码") or "").strip().upper() for p in tax_export_persons})
        person_units = get_person_units(conn, all_certs, pay_months)
        if exclude_certs or filter_handlers or filter_units:
            add_rows, departed_rows, pending_rows, stats = compare_personnel(
                tax_export_persons, payroll_certs, payroll_personnel, termination_dates,
                unpaid_persons=unpaid_persons, contract_signed_persons=contract_persons,
                person_units=person_units, filter_handlers=filter_handlers,
                filter_units=filter_units, exclude_certs=exclude_certs)
        # 补充增员人员详细信息 (未发薪/合同签署人员不在 payroll_personnel 中)
        if stats["add_count"] > len(add_rows):
            from templates_gen.personnel_compare import IDX_证件号码, map_personnel_info_to_row
            tax_cert_set = {str(p.get("证件号码") or "").strip().upper()
                            for p in tax_export_persons}
            all_add_certs = (payroll_certs | unpaid_persons | contract_persons) - tax_cert_set
            have_certs = {r[IDX_证件号码] for r in add_rows}
            need_certs = all_add_certs - have_certs
            if need_certs:
                extra_people = get_personnel_by_certs(conn, need_certs)
                for p in extra_people:
                    row = map_personnel_info_to_row(p)
                    info = (person_units or {}).get(str(p.身份证 or "").strip().upper())
                    if info and not row[25]:
                        row[25] = info.get("unit_name", "")
                    add_rows.append(row)
        month_label = str(pay_month)
        result = generate_compare_excel(add_rows, departed_rows, pending_rows, stats, OUTPUT_DIR, month_label)
        return jsonify({
            "add_count": stats["add_count"],
            "departed_count": stats["departed_count"],
            "pending_count": stats["pending_count"],
            "tax_total": stats["tax_total"],
            "payroll_total": stats["payroll_total"],
            "file_name": os.path.basename(result.file_path),
            "download_url": f"/api/download/{os.path.basename(result.file_path)}",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("FLASK_DEBUG", "0") == "1")