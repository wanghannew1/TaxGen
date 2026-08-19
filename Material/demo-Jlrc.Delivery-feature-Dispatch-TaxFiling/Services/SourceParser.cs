using NPOI.HSSF.UserModel;
using NPOI.SS.UserModel;
using TaxFilingService.Models;

namespace TaxFilingService.Services;

public class SourceParser
{
    public ParseResult Parse(Stream stream, string fileName)
    {
        var result = new ParseResult();
        using var workbook = new HSSFWorkbook(stream);
        var sheet = workbook.GetSheetAt(0);

        result.Logs.Add(new LogEntry { Stage = "读取文件", Type = "info", Message = $"打开文件 {fileName}，共 {sheet.LastRowNum + 1} 行" });

        var titleRow = sheet.GetRow(0);
        result.Title = titleRow?.GetCell(0)?.ToString()?.Trim() ?? "";
        result.Logs.Add(new LogEntry { Stage = "识别标题", Type = "info", Message = $"标题行: {result.Title}" });

        var headerRow = FindRow(sheet, 姓名_身份证_职工号);
        if (headerRow == null)
        {
            result.Error = "未找到表头行（需要包含：姓名、身份证、职工号）";
            result.Logs.Add(new LogEntry { Stage = "查找表头", Type = "error", Message = "在前15行中未找到包含'姓名'的表头行" });
            return result;
        }
        result.Logs.Add(new LogEntry { Stage = "查找表头", Type = "info", Message = $"在第 {headerRow.RowNum + 1} 行找到表头" });

        var map = BuildColumnMap(headerRow);
        result.Logs.Add(new LogEntry { Stage = "列映射", Type = "info", Message = $"主表头映射: {string.Join(", ", map.OrderBy(x=>x.Value).Select(x => $"{x.Key}→列{x.Value}"))}" });

        var deductionCatRow = FindDeductionCategoryRow(sheet, headerRow.RowNum + 1);
        var deductionMap = BuildDeductionColumnMap(deductionCatRow);
        if (deductionCatRow != null)
        {
            result.Logs.Add(new LogEntry { Stage = "扣款明细", Type = "info", Message = $"在第 {deductionCatRow.RowNum + 1} 行找到扣款明细分类行" });
            result.Logs.Add(new LogEntry { Stage = "扣款明细", Type = "info", Message = $"个人列映射: 养老→列{deductionMap.GetValueOrDefault("养老",0)}, 失业→列{deductionMap.GetValueOrDefault("失业",0)}, 医疗→列{deductionMap.GetValueOrDefault("医疗",0)}, 公积金→列{deductionMap.GetValueOrDefault("公积金",0)}" });
        }
        else
        {
            result.Logs.Add(new LogEntry { Stage = "扣款明细", Type = "warn", Message = "未找到扣款明细分类行（养老/失业/医疗/公积金）" });
        }

        var optionalMap = FindOptionalColumns(sheet, headerRow.RowNum);
        var optNames = new[] { "独生子女费", "大病险", "补缴及退款保险金额" };
        foreach (var t in optNames)
        {
            if (optionalMap.ContainsKey(t))
                result.Logs.Add(new LogEntry { Stage = "可选列", Type = "info", Message = $"找到可选列 '{t}' → 列{optionalMap[t]}" });
            else
                result.Logs.Add(new LogEntry { Stage = "可选列", Type = "warn", Message = $"未找到可选列 '{t}'，该字段将为空" });
        }

        result.Logs.Add(new LogEntry { Stage = "解析数据", Type = "info", Message = $"从第 {headerRow.RowNum + 2} 行开始解析数据..." });

        for (int r = headerRow.RowNum + 1; r <= sheet.LastRowNum; r++)
        {
            var row = sheet.GetRow(r);
            if (row == null) continue;

            var name = GetString(row, map, "姓名");
            if (string.IsNullOrEmpty(name)) continue;
            var id = GetString(row, map, "身份证");
            if (string.IsNullOrEmpty(id)) continue;

            var rec = new SourceRecord
            {
                序号 = GetString(row, map, "序号") ?? "",
                姓名 = name,
                身份证 = id,
                部门 = GetString(row, map, "部门") ?? "",
                岗位 = GetString(row, map, "岗位") ?? "",
                职工号 = GetString(row, map, "职工号") ?? "",
                基本工资 = GetDecimal(row, map, "基本工资"),
                扣款 = GetDecimal(row, map, "扣款"),
                奖金 = GetDecimal(row, map, "奖金"),
                岗位工资 = GetDecimal(row, map, "岗位工资"),
                绩效奖金 = GetDecimal(row, map, "绩效奖金"),
                津贴 = GetDecimal(row, map, "津贴"),
                补发 = GetDecimal(row, map, "补发"),
                采暖费 = GetDecimal(row, map, "采暖费"),
                补贴 = GetDecimal(row, map, "补贴"),
                业绩津贴 = GetDecimal(row, map, "业绩津贴"),
                交通补贴 = GetDecimal(row, map, "交通补贴"),
                应发工资 = GetDecimal(row, map, "应发工资"),
                单位缴纳五险一金 = GetDecimal(row, map, "单位缴纳五险一金"),
                单位代理费 = GetDecimal(row, map, "单位代理费"),
                转账合计 = GetDecimal(row, map, "转账合计"),
                社保基数 = GetDecimal(row, map, "社保基数"),
                医保基数 = GetDecimal(row, map, "医保基数"),
                工伤基数 = GetDecimal(row, map, "工伤基数"),
                公积金基数 = GetDecimal(row, map, "公积金基数"),
                个人所得税 = GetDecimal(row, map, "个人所得税"),
                实发工资 = GetDecimal(row, map, "实发工资"),
                实发合计 = GetDecimal(row, map, "实发合计"),
            };

            rec.养老个人 = GetDeductionDecimal(row, deductionMap, "养老");
            rec.失业个人 = GetDeductionDecimal(row, deductionMap, "失业");
            rec.医疗个人 = GetDeductionDecimal(row, deductionMap, "医疗");
            rec.公积金个人 = GetDeductionDecimal(row, deductionMap, "公积金");

            rec.补缴及退款保险金额个人 = GetCellDecimalByMap(row, optionalMap, "补缴及退款保险金额");
            rec.大病险个人 = GetCellDecimalByMap(row, optionalMap, "大病险");
            rec.独生子女费 = GetCellDecimalByMap(row, optionalMap, "独生子女费");

            result.Records.Add(rec);
        }

        result.Logs.Add(new LogEntry { Stage = "解析数据", Type = "info", Message = $"共提取 {result.Records.Count} 条记录" });
        if (result.Records.Count > 0)
        {
            var first = result.Records[0];
            result.Logs.Add(new LogEntry { Stage = "数据样本", Type = "info", Message = $"第1条: {first.姓名} | 应发工资={first.应发工资} | 实发工资={first.实发工资} | 养老个人={first.养老个人} | 医疗个人={first.医疗个人} | 失业个人={first.失业个人} | 公积金个人={first.公积金个人}" });
        }

        return result;
    }

    private static IRow? FindRow(ISheet sheet, Func<IRow, bool> predicate)
    {
        for (int r = 0; r <= Math.Min(sheet.LastRowNum, 15); r++)
        {
            var row = sheet.GetRow(r);
            if (row == null) continue;
            if (predicate(row))
                return row;
        }
        return null;
    }

    private static bool 姓名_身份证_职工号(IRow row)
    {
        for (int c = 0; c < 20; c++)
        {
            var val = row.GetCell(c)?.ToString()?.Trim() ?? "";
            if (val == "姓名") return true;
        }
        return false;
    }

    private static Dictionary<string, int> BuildColumnMap(IRow row)
    {
        var map = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        for (int c = 0; c < 60; c++)
        {
            var val = row.GetCell(c)?.ToString()?.Trim();
            if (!string.IsNullOrEmpty(val))
            {
                val = val.Replace("*", "");
                if (!map.ContainsKey(val))
                    map[val] = c;
            }
        }
        return map;
    }

    private static Dictionary<string, int> FindOptionalColumns(ISheet sheet, int headerRowNum)
    {
        var targets = new[] { "独生子女费", "大病险", "补缴及退款保险金额" };
        var map = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        for (int r = Math.Max(0, headerRowNum - 2); r <= Math.Min(sheet.LastRowNum, headerRowNum + 5); r++)
        {
            var row = sheet.GetRow(r);
            if (row == null) continue;
            for (int c = 0; c < 50; c++)
            {
                var val = row.GetCell(c)?.ToString()?.Trim() ?? "";
                foreach (var t in targets)
                {
                    if (val.Contains(t) && !map.ContainsKey(t))
                        map[t] = c;
                }
            }
        }
        return map;
    }

    private static decimal? GetCellDecimalByMap(IRow row, Dictionary<string, int> map, string key)
    {
        if (!map.TryGetValue(key, out var col)) return null;
        return GetCellDecimal(row, col);
    }

    private IRow? FindDeductionCategoryRow(ISheet sheet, int startRow)
    {
        for (int r = startRow; r <= Math.Min(sheet.LastRowNum, startRow + 5); r++)
        {
            var row = sheet.GetRow(r);
            if (row == null) continue;
            for (int c = 0; c < 40; c++)
            {
                var val = row.GetCell(c)?.ToString()?.Trim();
                if (val == "养老" || val == "失业")
                    return row;
            }
        }
        return null;
    }

    private static Dictionary<string, int> BuildDeductionColumnMap(IRow? row)
    {
        var result = new Dictionary<string, int>();
        if (row == null) return result;
        for (int c = 0; c <= 40; c++)
        {
            var val = row.GetCell(c)?.ToString()?.Trim();
            if (string.IsNullOrEmpty(val)) continue;
            if (val is "养老" or "失业" or "事业" or "医疗" or "公积金")
                result[val] = c + 1;
        }
        return result;
    }

    private static decimal? GetDeductionDecimal(IRow row, Dictionary<string, int> map, string key)
    {
        if (!map.TryGetValue(key, out var col))
        {
            if (key == "失业" && map.TryGetValue("事业", out col))
                return GetCellDecimal(row, col);
            return null;
        }
        return GetCellDecimal(row, col);
    }

    private static string? GetString(IRow row, Dictionary<string, int> map, string key)
    {
        if (!map.TryGetValue(key, out var col)) return null;
        return row.GetCell(col)?.ToString()?.Trim();
    }

    private static decimal? GetDecimal(IRow row, Dictionary<string, int> map, string key)
    {
        if (!map.TryGetValue(key, out var col)) return null;
        return GetCellDecimal(row, col);
    }

    private static decimal? GetCellDecimal(IRow row, int col)
    {
        var cell = row.GetCell(col);
        if (cell == null) return null;
        var val = cell.ToString()?.Trim();
        if (string.IsNullOrEmpty(val)) return null;
        if (decimal.TryParse(val.Replace(",", "").Replace(" ", ""), out var d))
            return d;
        return null;
    }
}

public class ParseResult
{
    public string Title { get; set; } = "";
    public List<SourceRecord> Records { get; set; } = new();
    public List<LogEntry> Logs { get; set; } = new();
    public string? Error { get; set; }
}
