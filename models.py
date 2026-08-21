from dataclasses import dataclass
from typing import Optional

@dataclass
class SalaryRecord:
    """工资记录 - 等效于 demo 的 SourceRecord"""
    # 基础信息
    职工号: str = ""  # AAC001
    姓名: str = ""  # AAC003
    身份证: str = ""  # AC002 from AC01
    工资所属年月: int = 0  # ATC931
    结算单元: int = 0  # ATB930
    当月批次: str = ""  # ATC937

    # 工资汇总
    应发工资: float = 0  # ATC933
    实发工资: float = 0  # ATC93C
    个人所得税: float = 0  # ATC93D
    工资总额: float = 0  # ATC93AA
    独生子女费: float = 0  # ATC93W4
    采暖费: float = 0  # ATC93W21
    奖金: float = 0  # ATC93W1

    # 五险一金个人部分 (from TC93 columns, verified in Task 3)
    养老个人: float = 0  # BAA001 (8%)
    医疗个人: float = 0  # BAA002 (2%)
    失业个人: float = 0  # BAA003 (0.3%)
    公积金个人: float = 0  # CAA002 (7%)

    # 补缴/大病险 (confirmed via Oracle all_col_comments on TC93)
    补缴及退款保险金额个人: float = 0  # ATC93BE
    大病险个人: float = 0  # ATC93BD
    补发3: float = 0  # ATC936 - 第三类补发扣款

    # 验证字段
    本期收入: float = 0  # 计算得出
    免税: float = 0  # 计算得出

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
