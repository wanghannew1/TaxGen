"""共享公式模块 - 本期收入和免税的统一计算

本期收入公式 (经 data-logic-discovery.md 验证, 精确匹配率 90.7%):
    本期收入 = 工资总额 - 补发3 - 大病险(个人)

免税公式:
    免税 = 大病险(个人) + 采暖费 + 独生子女费
"""
from models import SalaryRecord


def calc_本期收入(rec: SalaryRecord) -> float:
    """本期收入 = 工资总额 - 补发3 - 大病险(个人)

    字段含义:
    - 工资总额(ATC93AA): 应发工资（含基本工资、绩效、补贴等）
    - 补发3(ATC936): 第三类补发扣款（如采暖费、独生子女费等专项扣款）
    - 大病险个人(ATC93BD): 大病医疗互助补充保险个人缴纳部分
    """
    return rec.工资总额 - rec.补发3 - rec.大病险个人


def calc_免税(rec: SalaryRecord) -> float:
    """免税 = 大病险(个人) + 采暖费 + 独生子女费"""
    return rec.大病险个人 + rec.采暖费 + rec.独生子女费
