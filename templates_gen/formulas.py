"""共享公式模块 - 本期收入和免税的统一计算

本期收入公式 (经 data-logic-discovery.md 验证, 精确匹配率 90.7%):
    本期收入 = 工资总额 - 本次免税 - 大病险(个人)

免税公式:
    免税 = 本次免税(ATC936) + 大病险(个人)
"""
from decimal import Decimal
from models import SalaryRecord


def calc_本期收入(rec: SalaryRecord) -> Decimal:
    """本期收入 = 工资总额 − 本次免税 − 大病险(个人)"""
    return rec.工资总额 - rec.补发3 - rec.大病险个人


def calc_免税(rec: SalaryRecord) -> Decimal:
    """免税 = 本次免税(ATC936) + 大病险个人(ATC93BD)"""
    return rec.补发3 + rec.大病险个人
