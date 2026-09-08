"""数据验证模块 - 精确复制 demo TemplateFiller.cs 验证算法"""
from typing import List
from models import SalaryRecord, ValidationReport
from templates_gen.formulas import calc_本期收入


def validate_salary_records(records: List[SalaryRecord]) -> ValidationReport:
    """验证工资记录 - 左=右 校验

    验证公式:
        左 = 本期收入 - 养老个人 - 失业个人 - 医疗个人 - 公积金个人 - 意外险个人 + 本次免税(ATC936)
        右 = (实发工资 - 经济补偿金) + 税后工会会费 + 个人代理费 + 个人所得税 + 个人其他调整(ATC93AG)
        通过 = |左 - 右| < 0.01
        说明: 本期收入内部已减本次免税与大病险个人(ATC93BD)，左式加回本次免税回到工资总额口径；
              大病险个人左右两侧同项（收入已减、实发已刨除）销项不单列。
    """
    pass_count = 0
    fail_count = 0
    details = []
    
    for rec in records:
        income = calc_本期收入(rec)
        
        left = income - rec.养老个人 - rec.失业个人 - rec.医疗个人 - rec.公积金个人 \
               - rec.意外险个人 + rec.补发3
        right = (rec.实发工资 - rec.经济补偿金) + rec.税后工会会费 + rec.个人代理费 + rec.个人所得税 + rec.个人其他调整
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
                "其他调整": rec.个人其他调整, "个人欠款": rec.个人欠款,
                "实发": rec.实发工资, "工会会费": rec.税后工会会费, "代理费": rec.个人代理费,
                "个税": rec.个人所得税,
            })
    
    return ValidationReport(
        total_count=len(records),
        pass_count=pass_count,
        fail_count=fail_count,
        details=details
    )
