"""共享公式模块 - 本期收入和免税的统一计算

本期收入公式 (经 data-logic-discovery.md 验证, 精确匹配率 90.7%):
    本期收入 = 工资总额 - 本次免税 - 大病险(个人)

免税公式:
    免税 = 本次免税(ATC936) + 大病险(个人)
"""
from decimal import Decimal
from models import SalaryRecord


def calc_本期收入(rec: SalaryRecord) -> Decimal:
    """本期收入 = 工资总额 − 本次免税 − 大病险(个人) − 补缴及退款保险差额(个人) + 个人交纳现金(ATC93X3) − 个人欠款(ATC93E)

    个人交纳现金(ATC93X3): 现金交纳的社保等, 计入本期收入, 同时对应五险一金列抵扣。
    个人欠款(ATC93E): 欠款冲抵收入（零工资补缴月份 E 为负值, 抵消累计补缴, 如马静 -1825.5+2434=608.5）。
    与个税端回盘口径一致（宋红玥 应发0 交纳现金608.5 -> 报税收入608.5）。
    """
    return (rec.工资总额 - rec.补发3 - rec.大病险个人 - rec.补缴及退款保险金额个人
            + rec.个人交纳现金 - rec.个人欠款)


def calc_免税(rec: SalaryRecord) -> Decimal:
    """免税 = 本次免税(ATC936) + 大病险个人(ATC93BD)"""
    return rec.补发3 + rec.大病险个人
