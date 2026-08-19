namespace TaxFilingService.Models;

public class SourceRecord
{
    public string 序号 { get; set; } = "";
    public string 姓名 { get; set; } = "";
    public string 身份证 { get; set; } = "";
    public string 部门 { get; set; } = "";
    public string 岗位 { get; set; } = "";
    public string 职工号 { get; set; } = "";
    public decimal? 基本工资 { get; set; }
    public decimal? 扣款 { get; set; }
    public decimal? 奖金 { get; set; }
    public decimal? 岗位工资 { get; set; }
    public decimal? 绩效奖金 { get; set; }
    public decimal? 津贴 { get; set; }
    public decimal? 补发 { get; set; }
    public decimal? 采暖费 { get; set; }
    public decimal? 补贴 { get; set; }
    public decimal? 业绩津贴 { get; set; }
    public decimal? 交通补贴 { get; set; }
    public decimal? 应发工资 { get; set; }
    public decimal? 单位缴纳五险一金 { get; set; }
    public decimal? 单位代理费 { get; set; }
    public decimal? 转账合计 { get; set; }
    public decimal? 社保基数 { get; set; }
    public decimal? 医保基数 { get; set; }
    public decimal? 工伤基数 { get; set; }
    public decimal? 公积金基数 { get; set; }
    public decimal? 养老单位 { get; set; }
    public decimal? 养老个人 { get; set; }
    public decimal? 失业单位 { get; set; }
    public decimal? 失业个人 { get; set; }
    public decimal? 医疗单位 { get; set; }
    public decimal? 医疗个人 { get; set; }
    public decimal? 工伤险 { get; set; }
    public decimal? 公积金单位 { get; set; }
    public decimal? 公积金个人 { get; set; }
    public decimal? 公务员医疗补助 { get; set; }
    public decimal? 单位代理费扣款明细 { get; set; }
    public decimal? 扣款合计 { get; set; }
    public decimal? 个人所得税 { get; set; }
    public decimal? 实发工资 { get; set; }
    public decimal? 实发合计 { get; set; }
    public decimal? 补缴及退款保险金额个人 { get; set; }
    public decimal? 大病险个人 { get; set; }
    public decimal? 独生子女费 { get; set; }
}
