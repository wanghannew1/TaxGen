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
from templates_gen.personnel_compare import compare_personnel, generate_compare_excel
from templates_gen.tax_export_parser import parse_tax_export
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


@app.route("/api/personnel-compare/compare", methods=["POST"])
def api_personnel_compare():
    """上传个税端导出文件 + 选择发放月份 → 增减员比对 → 生成 Excel。"""
    try:
        from queries import get_payroll_cert_numbers, get_tc90_termination_dates, get_payroll_personnel
        from templates_gen.personnel_compare import compare_personnel, generate_compare_excel
        from templates_gen.tax_export_parser import parse_tax_export

        file = request.files.get("file")
        if not file:
            return jsonify({"error": "请上传个税端导出文件"}), 400
        month = int(request.form.get("month", 0) or 0)
        if not month:
            return jsonify({"error": "请选择发放月份"}), 400
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
        payroll_certs = get_payroll_cert_numbers(conn, month)
        payroll_personnel = get_payroll_personnel(conn, month)
        termination_dates = get_tc90_termination_dates(conn, payroll_certs)
        add_rows, remove_rows, stats = compare_personnel(
            tax_export_persons, payroll_certs, payroll_personnel, termination_dates)
        result = generate_compare_excel(add_rows, remove_rows, stats, OUTPUT_DIR, month)
        return jsonify({
            "add_count": stats["add_count"],
            "remove_count": stats["remove_count"],
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