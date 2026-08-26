from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

@dataclass
class SalaryRecord:
    """工资记录 - 等效于 demo 的 SourceRecord"""
    # 基础信息
    职工号: str = ""  # AAC001 - 个人编号
    姓名: str = ""  # AAC003 - 姓名
    身份证: str = ""  # AAC002 - 公民身份号码 (from AC01)
    工资所属年月: int = 0  # ATC931 - 工资所属年月
    结算单元: int = 0  # ATB930 - 结算单元流水号
    当月批次: str = ""  # ATC937 - 工资发放次数
    tc930_id: int = 0  # ATC930 - 流水号

    # 工资汇总
    应发工资: Decimal = Decimal("0")  # ATC933 - 本次应发工资
    实发工资: Decimal = Decimal("0")  # ATC93C - 本次实发金额(合计)
    个人所得税: Decimal = Decimal("0")  # ATC93D - 本次个人所得税
    工资总额: Decimal = Decimal("0")  # ATC93AA - 本次工资总额
    独生子女费: Decimal = Decimal("0")  # ATC93W4 - 独生子女费
    采暖费: Decimal = Decimal("0")  # ATC93W21 - 采暖费
    奖金: Decimal = Decimal("0")  # ATC93W1 - 奖金

    # 五险一金个人部分
    养老个人: Decimal = Decimal("0")  # BAA001 - 当月养老个人缴 (8%)
    医疗个人: Decimal = Decimal("0")  # BAA002 - 当月医疗个人缴 (2%)
    失业个人: Decimal = Decimal("0")  # BAA003 - 当月失业个人缴 (0.3%)
    公积金个人: Decimal = Decimal("0")  # CAA002 - 个人公积金月缴存额 (7%)

    # 补缴/大病险/免税
    补缴及退款保险金额个人: Decimal = Decimal("0")  # ATC93BE - 补缴及退款保险差额（个人）
    大病险个人: Decimal = Decimal("0")  # ATC93BD - 大病险（个人承担）
    补发3: Decimal = Decimal("0")  # ATC936 - 本次免税 (= 采暖费 + 独生子女费)

    # 验证辅助字段
    个人其他调整: Decimal = Decimal("0")  # ATC93AG
    个人欠款: Decimal = Decimal("0")  # ATC93E
    扣款大病险: Decimal = Decimal("0")  # ATC93Y2 - 扣款-大病险
    税后工会会费: Decimal = Decimal("0")  # ATC93Z2
    个人代理费: Decimal = Decimal("0")  # BAA300
    意外险个人: Decimal = Decimal("0")  # ATC93BH - 意外险（个人承担）

    # 验证字段
    本期收入: Decimal = Decimal("0")  # 计算得出
    免税: Decimal = Decimal("0")  # 计算得出

@dataclass
class PersonnelInfo:
    """人员信息 - 用于人员信息采集导入模板"""
    职工号: str = ""
    姓名: str = ""
    身份证: str = ""
    性别: str = ""  # 从身份证第17位解析
    出生日期: str = ""  # 从身份证第7-14位提取
    证件类型: str = "居民身份证"
    国籍: str = "中国"
    手机号码: str = ""
    任职类型: str = "雇员"

@dataclass
class MonthOption:
    """月份选项 - 用于下拉框"""
    value: int  # YYYYMM
    label: str  # 显示文本

@dataclass
class GenerateRequest:
    """生成请求"""
    month: int
    templates: list  # ["normalSalary", "laborService", "annualBonus", "personnelInfo"]

@dataclass
class GenerateResult:
    """生成结果"""
    file_path: str
    template_type: str
    record_count: int
    validation_pass: int
    validation_fail: int

@dataclass
class ValidationReport:
    """验证报告"""
    total_count: int
    pass_count: int
    fail_count: int
    details: list  # List of failed record details
