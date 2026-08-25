"""Flask 主应用 - 个税模板填表工具"""
import atexit
import os
from flask import Flask, render_template, request, jsonify, send_file
from db import init_db, get_connection, close_db
from queries import get_available_months, get_salary_records, get_personnel_info, get_suggestions, search_tc8m, get_abnormal_records, get_tc93_all_fields, get_tc93_field_comments
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
                                           tc93_comments=get_tc93_field_comments(conn))
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
            "tc93_total_count": len(tc93_all)
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

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)