"""Flask 主应用 - 个税模板填表工具"""
import atexit
import os
from flask import Flask, render_template, request, jsonify, send_file
from db import init_db, get_connection, close_db
from queries import get_available_months, get_salary_records, get_personnel_info
from templates_gen.normal_salary import generate_normal_salary
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

@app.route("/api/generate", methods=["POST"])
def api_generate():
    try:
        data = request.get_json()
        month = data.get("month")
        templates = data.get("templates", [])
        
        if not month:
            return jsonify({"error": "请选择月份"}), 400
        if not templates:
            return jsonify({"error": "请选择至少一个模板"}), 400
        
        conn = get_connection()
        records = get_salary_records(conn, month)
        personnel = get_personnel_info(conn, month)
        
        if not records:
            return jsonify({"error": f"月份 {month} 无工资数据"}), 400
        
        results = []
        for tpl in templates:
            if tpl == "normalSalary":
                r = generate_normal_salary(records, f"劳务派遣人员工资发放表{month}", OUTPUT_DIR)
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
        
        return jsonify({"files": results})
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