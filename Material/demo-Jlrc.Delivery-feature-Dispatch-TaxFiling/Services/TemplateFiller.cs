using NPOI.XSSF.UserModel;
using TaxFilingService.Models;

namespace TaxFilingService.Services;

public class TemplateFiller
{
    private readonly string _templatesDir;

    public TemplateFiller(string templatesDir)
    {
        _templatesDir = templatesDir;
    }

    public async Task<ConvertResult> FillNormalSalaryAsync(List<SourceRecord> records, string title, string outputDir)
    {
        var outputPath = Path.Combine(outputDir, $"正常工资薪金所得_{DateTime.Now:yyyyMMddHHmmss}.xlsx");
        var result = new ConvertResult { OutputPath = outputPath, TemplateType = "正常工资薪金所得" };

        var remark = ExtractRemark(title);
        result.Logs.Add(new LogEntry { Stage = "正常工资薪金|初始化", Type = "info", Message = $"备注字段: '{remark}'（从标题提取）" });

        var workbook = new XSSFWorkbook();
        var ws = workbook.CreateSheet("正常工资薪金收入");

        var headers = new[]
        {
            "工号", "*姓名", "*证件类型", "*证件号码", "本期收入", "本期免税收入",
            "基本养老保险费", "基本医疗保险费", "失业保险费", "住房公积金",
            "累计子女教育", "累计继续教育", "累计住房贷款利息", "累计住房租金",
            "累计赡养老人", "累计3岁以下婴幼儿照护", "累计个人养老金", "企业(职业)年金",
            "商业健康保险", "税延养老保险", "公务交通费用", "通讯费用", "律师办案费用",
            "西藏附加减除费用", "其他", "准予扣除的捐赠额", "减免税额", "协定减免", "备注"
        };
        var headerRow = ws.CreateRow(0);
        for (int i = 0; i < headers.Length; i++)
            headerRow.CreateCell(i).SetCellValue(headers[i]);
        result.Logs.Add(new LogEntry { Stage = "正常工资薪金|初始化", Type = "info", Message = $"输出模板共 {headers.Length} 列" });
        result.Logs.Add(new LogEntry { Stage = "正常工资薪金|规则", Type = "info", Message = "本期收入 = 应发工资 - 补缴及退款保险(个人) - 大病险(个人) - 采暖费 - 独生子女费" });
        result.Logs.Add(new LogEntry { Stage = "正常工资薪金|规则", Type = "info", Message = "基本养老保险费=扣款明细→养老→个人, 失业保险费=扣款明细→失业→个人, 基本医疗保险费=扣款明细→医疗→个人, 住房公积金=扣款明细→公积金→个人, 企业(职业)年金=0" });

        int rowIdx = 1;
        int idx = 0;
        foreach (var rec in records)
        {
            idx++;
            var income = Calc本期收入(rec);
            var taxExempt = Calc免税(rec);

            result.Logs.Add(new LogEntry { Stage = $"正常工资薪金|{idx}/{records.Count}", Type = "info",
                Message = $"{rec.姓名}({rec.职工号}): 应发={rec.应发工资} - 补缴={rec.补缴及退款保险金额个人} - 大病={rec.大病险个人} - 采暖={rec.采暖费} - 独生={rec.独生子女费} → 本期收入={income}" });

            var row = ws.CreateRow(rowIdx++);
            row.CreateCell(0).SetCellValue(rec.职工号 ?? "");
            row.CreateCell(1).SetCellValue(rec.姓名 ?? "");
            row.CreateCell(2).SetCellValue("居民身份证");
            row.CreateCell(3).SetCellValue(rec.身份证 ?? "");
            row.CreateCell(4).SetCellValue((double)income);
            if (taxExempt > 0) row.CreateCell(5).SetCellValue((double)taxExempt);
            row.CreateCell(6).SetCellValue((double)(rec.养老个人 ?? 0));
            row.CreateCell(7).SetCellValue((double)(rec.医疗个人 ?? 0));
            row.CreateCell(8).SetCellValue((double)(rec.失业个人 ?? 0));
            row.CreateCell(9).SetCellValue((double)(rec.公积金个人 ?? 0));
            row.CreateCell(17).SetCellValue(0);
            row.CreateCell(28).SetCellValue(remark);
        }

        result.Logs.Add(new LogEntry { Stage = "正常工资薪金|写入完成", Type = "info", Message = $"共写入 {records.Count} 条记录" });

        result.Logs.Add(new LogEntry { Stage = "正常工资薪金|验证规则", Type = "info", Message = "左 = 本期收入 - 养老 - 失业 - 医疗 - 公积金 - 年金(0)" });
        result.Logs.Add(new LogEntry { Stage = "正常工资薪金|验证规则", Type = "info", Message = "右 = 实发工资 + 个税 - (大病险个人 + 采暖费 + 独生子女费)" });

        int passCount = 0, failCount = 0;
        int vIdx = 0;
        foreach (var rec in records)
        {
            vIdx++;
            var income = Calc本期收入(rec);
            var left = income - (rec.养老个人 ?? 0) - (rec.失业个人 ?? 0)
                       - (rec.医疗个人 ?? 0) - (rec.公积金个人 ?? 0) - 0;
            var right = (rec.实发工资 ?? 0) + (rec.个人所得税 ?? 0) - Calc免税(rec);
            var diff = Math.Abs(left - right);
            var passed = diff < 0.01m;
            if (passed) passCount++; else failCount++;
            result.Validations.Add(new ValidationDetail
            {
                姓名 = rec.姓名,
                本期收入 = income,
                左 = left,
                右 = right,
                差值 = diff,
                通过 = passed,
            });
            result.Logs.Add(new LogEntry { Stage = $"正常工资薪金|验证{vIdx}/{records.Count}", Type = passed ? "info" : "warn",
                Message = $"{rec.姓名}: 左={left:F2} 右={right:F2} → {(passed ? "无差值" : $"差值={diff:F2}")}" });
        }

        result.Logs.Add(new LogEntry { Stage = "正常工资薪金|验证汇总", Type = failCount > 0 ? "warn" : "info", Message = $"验证完成: {passCount} 通过, {failCount} 不通过" });

        await WriteWorkbookAsync(workbook, outputPath);
        result.Logs.Add(new LogEntry { Stage = "正常工资薪金|保存", Type = "info", Message = $"已保存: {Path.GetFileName(outputPath)}" });

        return result;
    }

    public async Task<ConvertResult> FillLaborServiceAsync(List<SourceRecord> records, string title, string outputDir)
    {
        var outputPath = Path.Combine(outputDir, $"劳务报酬所得_{DateTime.Now:yyyyMMddHHmmss}.xlsx");
        var result = new ConvertResult { OutputPath = outputPath, TemplateType = "劳务报酬所得" };

        var remark = ExtractRemark(title);
        result.Logs.Add(new LogEntry { Stage = "劳务报酬|初始化", Type = "info", Message = $"备注: '{remark}'" });
        result.Logs.Add(new LogEntry { Stage = "劳务报酬|规则", Type = "info", Message = "所得项目=劳务报酬(固定), 收入=应发工资" });

        var workbook = new XSSFWorkbook();
        var ws = workbook.CreateSheet("劳务报酬");

        var headers = new[]
        {
            "工号", "*姓名", "*证件类型", "*证件号码", "*所得项目", "*收入",
            "免税收入", "商业健康保险", "税延养老保险", "其他",
            "允许扣除的税费", "减免税额", "协定减免", "备注"
        };
        var headerRow = ws.CreateRow(0);
        for (int i = 0; i < headers.Length; i++)
            headerRow.CreateCell(i).SetCellValue(headers[i]);
        result.Logs.Add(new LogEntry { Stage = "劳务报酬|初始化", Type = "info", Message = $"输出模板共 {headers.Length} 列" });

        int rowIdx = 1;
        int idx = 0;
        foreach (var rec in records)
        {
            idx++;
            result.Logs.Add(new LogEntry { Stage = $"劳务报酬|{idx}/{records.Count}", Type = "info",
                Message = $"{rec.姓名}({rec.职工号}): 所得项目=劳务报酬, 收入={rec.应发工资}" });

            var row = ws.CreateRow(rowIdx++);
            row.CreateCell(0).SetCellValue(rec.职工号 ?? "");
            row.CreateCell(1).SetCellValue(rec.姓名 ?? "");
            row.CreateCell(2).SetCellValue("居民身份证");
            row.CreateCell(3).SetCellValue(rec.身份证 ?? "");
            row.CreateCell(4).SetCellValue("劳务报酬");
            row.CreateCell(5).SetCellValue((double)(rec.应发工资 ?? 0));
            row.CreateCell(13).SetCellValue(remark);
        }

        result.Logs.Add(new LogEntry { Stage = "劳务报酬|写入完成", Type = "info", Message = $"共写入 {records.Count} 条记录" });

        await WriteWorkbookAsync(workbook, outputPath);
        result.Logs.Add(new LogEntry { Stage = "劳务报酬|保存", Type = "info", Message = $"已保存: {Path.GetFileName(outputPath)}" });

        return result;
    }

    public async Task<ConvertResult> FillAnnualBonusAsync(List<SourceRecord> records, string title, string outputDir)
    {
        var outputPath = Path.Combine(outputDir, $"全年一次性奖金收入_{DateTime.Now:yyyyMMddHHmmss}.xlsx");
        var result = new ConvertResult { OutputPath = outputPath, TemplateType = "全年一次性奖金收入" };

        var remark = ExtractRemark(title);
        result.Logs.Add(new LogEntry { Stage = "全年一次性奖金|初始化", Type = "info", Message = $"备注: '{remark}'" });
        result.Logs.Add(new LogEntry { Stage = "全年一次性奖金|规则", Type = "info", Message = "全年一次性奖金额 = 数据源.奖金" });

        var workbook = new XSSFWorkbook();
        var ws = workbook.CreateSheet("全年一次性奖金");

        var headers = new[]
        {
            "工号", "*姓名", "*证件类型", "*证件号码", "*全年一次性奖金额",
            "免税收入", "其他", "准予扣除的捐赠额", "减免税额", "协定减免", "备注"
        };
        var headerRow = ws.CreateRow(0);
        for (int i = 0; i < headers.Length; i++)
            headerRow.CreateCell(i).SetCellValue(headers[i]);
        result.Logs.Add(new LogEntry { Stage = "全年一次性奖金|初始化", Type = "info", Message = $"输出模板共 {headers.Length} 列" });

        int rowIdx = 1;
        int idx = 0;
        foreach (var rec in records)
        {
            idx++;
            result.Logs.Add(new LogEntry { Stage = $"全年一次性奖金|{idx}/{records.Count}", Type = "info",
                Message = $"{rec.姓名}({rec.职工号}): 奖金={rec.奖金}" });

            var row = ws.CreateRow(rowIdx++);
            row.CreateCell(0).SetCellValue(rec.职工号 ?? "");
            row.CreateCell(1).SetCellValue(rec.姓名 ?? "");
            row.CreateCell(2).SetCellValue("居民身份证");
            row.CreateCell(3).SetCellValue(rec.身份证 ?? "");
            row.CreateCell(4).SetCellValue((double)(rec.奖金 ?? 0));
            row.CreateCell(10).SetCellValue(remark);
        }

        result.Logs.Add(new LogEntry { Stage = "全年一次性奖金|写入完成", Type = "info", Message = $"共写入 {records.Count} 条记录" });

        await WriteWorkbookAsync(workbook, outputPath);
        result.Logs.Add(new LogEntry { Stage = "全年一次性奖金|保存", Type = "info", Message = $"已保存: {Path.GetFileName(outputPath)}" });

        return result;
    }

    public async Task<ConvertResult> FillPersonnelInfoAsync(List<SourceRecord> records, string title, string outputDir)
    {
        var outputPath = Path.Combine(outputDir, $"人员信息采集导入模板_{DateTime.Now:yyyyMMddHHmmss}.xlsx");
        var result = new ConvertResult { OutputPath = outputPath, TemplateType = "人员信息采集导入模板" };

        var remark = ExtractRemark(title);
        result.Logs.Add(new LogEntry { Stage = "人员信息采集|初始化", Type = "info", Message = $"备注: '{remark}'" });
        result.Logs.Add(new LogEntry { Stage = "人员信息采集|规则", Type = "info", Message = "性别=身份证第17位(奇=男偶=女), 出生日期=身份证第7-14位, 证件类型=居民身份证, 国籍=中国, 任职类型=雇员" });

        var workbook = new XSSFWorkbook();
        var ws = workbook.CreateSheet("人员信息");

        var headers = new[]
        {
            "工号", "*姓名", "*证件类型", "*证件号码", "*国籍(地区)", "*性别",
            "*出生日期", "是否高级专家", "*任职受雇从业类型", "其他情况说明",
            "入职年度就业情形", "手机号码", "任职受雇从业日期", "离职日期",
            "是否离职后补发工资", "实际补发工资的月份", "是否残疾", "是否烈属",
            "是否孤老", "残疾证件类型", "残疾证号", "烈属证号", "是否扣除减除费用",
            "个人投资额", "个人投资比例(%)", "备注", "中文名", "涉税事由",
            "出生国家(地区)", "首次入境时间", "预计离境时间", "其他证件类型",
            "其他证件号码", "户籍所在地（省）", "户籍所在地（市）", "户籍所在地（区县）",
            "户籍所在地（详细地址）", "经常居住地（省）", "经常居住地（市）",
            "经常居住地（区县）", "经常居住地（详细地址）", "联系地址（省）",
            "联系地址（市）", "联系地址（区县）", "联系地址（详细地址）",
            "电子邮箱", "学历", "开户银行", "银行账号", "开户行省份", "职务"
        };
        var headerRow = ws.CreateRow(0);
        for (int i = 0; i < headers.Length; i++)
            headerRow.CreateCell(i).SetCellValue(headers[i]);
        result.Logs.Add(new LogEntry { Stage = "人员信息采集|初始化", Type = "info", Message = $"输出模板共 {headers.Length} 列" });

        int rowIdx = 1;
        int idx = 0;
        foreach (var rec in records)
        {
            idx++;
            var gender = GetGenderFromId(rec.身份证);
            var birthDate = GetBirthDateFromId(rec.身份证);
            result.Logs.Add(new LogEntry { Stage = $"人员信息采集|{idx}/{records.Count}", Type = "info",
                Message = $"{rec.姓名}({rec.职工号}): 身份证={rec.身份证}, 性别={gender}, 出生={birthDate}" });

            var row = ws.CreateRow(rowIdx++);
            row.CreateCell(0).SetCellValue(rec.职工号 ?? "");
            row.CreateCell(1).SetCellValue(rec.姓名 ?? "");
            row.CreateCell(2).SetCellValue("居民身份证");
            row.CreateCell(3).SetCellValue(rec.身份证 ?? "");
            row.CreateCell(4).SetCellValue("中国");
            row.CreateCell(5).SetCellValue(gender);
            if (birthDate != null) row.CreateCell(6).SetCellValue(birthDate);
            row.CreateCell(8).SetCellValue("雇员");
            row.CreateCell(25).SetCellValue(remark);
        }

        result.Logs.Add(new LogEntry { Stage = "人员信息采集|写入完成", Type = "info", Message = $"共写入 {records.Count} 条记录" });

        await WriteWorkbookAsync(workbook, outputPath);
        result.Logs.Add(new LogEntry { Stage = "人员信息采集|保存", Type = "info", Message = $"已保存: {Path.GetFileName(outputPath)}" });

        return result;
    }

    private static async Task WriteWorkbookAsync(XSSFWorkbook workbook, string outputPath)
    {
        using var fs = new FileStream(outputPath, FileMode.Create, FileAccess.Write);
        workbook.Write(fs);
        workbook.Close();
        await Task.CompletedTask;
    }

    private static string ExtractRemark(string title)
    {
        if (string.IsNullOrEmpty(title)) return "";
        var t = title.Replace("东北师范大学人事处", "").Replace("劳务派遣人员工资发放表", "").Trim();
        t = t.Replace("年", "").Replace("月", "").Replace("系统", "").Trim();
        var digits = new string(t.Where(char.IsDigit).ToArray());
        if (digits.Length >= 4)
            t = t.Replace(digits, "").Trim();
        return string.IsNullOrEmpty(t) ? title : t;
    }

    public static decimal Calc本期收入(SourceRecord rec)
    {
        var result = rec.应发工资 ?? 0;
        result -= rec.补缴及退款保险金额个人 ?? 0;
        result -= rec.大病险个人 ?? 0;
        result -= rec.采暖费 ?? 0;
        result -= rec.独生子女费 ?? 0;
        return result;
    }

    public static decimal Calc免税(SourceRecord rec)
    {
        return (rec.大病险个人 ?? 0) + (rec.采暖费 ?? 0) + (rec.独生子女费 ?? 0);
    }

    public static string GetValidationText(SourceRecord rec)
    {
        var income = Calc本期收入(rec);
        var left = income - (rec.养老个人 ?? 0) - (rec.失业个人 ?? 0)
                   - (rec.医疗个人 ?? 0) - (rec.公积金个人 ?? 0) - 0;
        var right = (rec.实发工资 ?? 0) + (rec.个人所得税 ?? 0) - Calc免税(rec);
        var diff = Math.Abs(left - right);
        var passed = diff < 0.01m;
        return $"本期收入={income:F2}, 养老={rec.养老个人:F2}, 失业={rec.失业个人:F2}, 医疗={rec.医疗个人:F2}, 公积金={rec.公积金个人:F2} | " +
               $"左={left:F2} = 右={right:F2} | {(passed ? "无差值" : $"差值={diff:F2}")} | {(passed ? "通过" : "不通过")}";
    }

    private static string GetGenderFromId(string id)
    {
        if (string.IsNullOrEmpty(id) || id.Length < 18) return "";
        return int.TryParse(id[16].ToString(), out var d) ? (d % 2 == 1 ? "男" : "女") : "";
    }

    private static string? GetBirthDateFromId(string id)
    {
        if (string.IsNullOrEmpty(id) || id.Length < 14) return null;
        var y = id.Substring(6, 4);
        var m = id.Substring(10, 2);
        var d = id.Substring(12, 2);
        return $"{y}{m}{d}";
    }
}
