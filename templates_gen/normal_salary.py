"""正常工资薪金所得模板生成器 - 精确复制 demo TemplateFiller.cs 算法"""
from datetime import datetime
from typing import List
import os
from openpyxl import Workbook
from models import SalaryRecord, GenerateResult
from templates_gen.formulas import calc_本期收入, calc_免税


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


def generate_normal_salary(records: List[SalaryRecord], title: str, output_dir: str) -> GenerateResult:
    """生成正常工资薪金所得 Excel 模板"""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    output_path = os.path.join(output_dir, f"正常工资薪金所得_{timestamp}.xlsx")
    remark = extract_remark(title)
    
    wb = Workbook()
    ws = wb.active
    ws.title = "正常工资薪金收入"
    
    # 29列标题 - 必须与 demo 完全一致
    headers = [
        "工号", "*姓名", "*证件类型", "*证件号码", "本期收入", "本期免税收入",
        "基本养老保险费", "基本医疗保险费", "失业保险费", "住房公积金",
        "累计子女教育", "累计继续教育", "累计住房贷款利息", "累计住房租金",
        "累计赡养老人", "累计3岁以下婴幼儿照护", "累计个人养老金", "企业(职业)年金",
        "商业健康保险", "税延养老保险", "公务交通费用", "通讯费用", "律师办案费用",
        "西藏附加减除费用", "其他", "准予扣除的捐赠额", "减免税额", "协定减免", "备注"
    ]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)
    
    # 写入数据
    validations = []
    for idx, rec in enumerate(records, 1):
        row = idx + 1
        income = calc_本期收入(rec)
        tax_exempt = calc_免税(rec)
        
        ws.cell(row=row, column=1, value=rec.职工号)
        ws.cell(row=row, column=2, value=rec.姓名)
        ws.cell(row=row, column=3, value="居民身份证")
        ws.cell(row=row, column=4, value=rec.身份证)
        ws.cell(row=row, column=5, value=income)
        if tax_exempt > 0:
            ws.cell(row=row, column=6, value=tax_exempt)
        ws.cell(row=row, column=7, value=rec.养老个人)
        ws.cell(row=row, column=8, value=rec.医疗个人)
        ws.cell(row=row, column=9, value=rec.失业个人)
        ws.cell(row=row, column=10, value=rec.公积金个人)
        ws.cell(row=row, column=18, value=0)  # 企业(职业)年金 = 0
        ws.cell(row=row, column=29, value=remark)
        
        # 验证: 左 = 本期收入 - 养老 - 失业 - 医疗 - 公积金 - 年金(0)
        #        右 = 实发 + 个税 - 免税
        left = income - rec.养老个人 - rec.失业个人 - rec.医疗个人 - rec.公积金个人 - 0
        right = rec.实发工资 + rec.个人所得税 - tax_exempt
        diff = abs(left - right)
        passed = diff < 0.01
        validations.append({
            "姓名": rec.姓名, "本期收入": income,
            "左": left, "右": right, "差值": diff, "通过": passed
        })
    
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