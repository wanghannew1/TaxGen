"""数据验证模块 - 精确复制 demo TemplateFiller.cs 验证算法"""
from typing import List
from models import SalaryRecord, ValidationReport
from templates_gen.formulas import calc_本期收入, calc_免税


def validate_salary_records(records: List[SalaryRecord]) -> ValidationReport:
    """验证工资记录 - 左=右 校验
    
    验证公式:
        左 = 本期收入 - 养老个人 - 失业个人 - 医疗个人 - 公积金个人 - 年金(0)
        右 = 实发工资 + 个人所得税 - 免税
        通过 = |左 - 右| < 0.01
    """
    pass_count = 0
    fail_count = 0
    details = []
    
    for rec in records:
        income = calc_本期收入(rec)
        tax_exempt = calc_免税(rec)
        
        left = income - rec.养老个人 - rec.失业个人 - rec.医疗个人 - rec.公积金个人 \
               - rec.个人其他调整 - rec.个人欠款 - rec.扣款大病险
        right = rec.实发工资 + rec.税后工会会费 + rec.个人代理费 + rec.个人所得税 - tax_exempt
        diff = abs(left - right)
        passed = diff < 0.01
        
        if passed:
            pass_count += 1
        else:
            fail_count += 1
            details.append({
                "姓名": rec.姓名,
                "职工号": rec.职工号,
                "本期收入": income,
                "左": left,
                "右": right,
                "差值": diff,
                "工资总额": rec.工资总额,
                "本次免税": rec.补发3,
                "大病险个人": rec.大病险个人,
                "补缴退款差额": rec.补缴及退款保险金额个人,
                "养老": rec.养老个人, "失业": rec.失业个人, "医疗": rec.医疗个人, "公积金": rec.公积金个人,
                "其他调整": rec.个人其他调整, "个人欠款": rec.个人欠款, "扣款大病险": rec.扣款大病险,
                "实发": rec.实发工资, "工会会费": rec.税后工会会费, "代理费": rec.个人代理费,
                "个税": rec.个人所得税, "免税": tax_exempt,
            })
    
    return ValidationReport(
        total_count=len(records),
        pass_count=pass_count,
        fail_count=fail_count,
        details=details
    )
