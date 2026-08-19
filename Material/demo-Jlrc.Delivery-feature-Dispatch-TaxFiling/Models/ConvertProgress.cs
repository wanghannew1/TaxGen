using System.Collections.Generic;
using TaxFilingService.Models;

namespace TaxFilingService.Models;

public class ConvertProgress
{
    public string TaskId { get; set; } = "";
    public string Status { get; set; } = "pending";
    public int Percent { get; set; }
    public string Message { get; set; } = "";
    public List<string> ResultPaths { get; set; } = new();
    public string? Error { get; set; }
    public List<LogEntry> Logs { get; set; } = new();
    public List<TemplateResultInfo> TemplateResults { get; set; } = new();

    public ConvertProgress Clone() => new()
    {
        TaskId = TaskId,
        Status = Status,
        Percent = Percent,
        Message = Message,
        ResultPaths = new List<string>(ResultPaths),
        Error = Error,
        Logs = new List<LogEntry>(Logs),
        TemplateResults = TemplateResults.Select(t => new TemplateResultInfo
        {
            Type = t.Type,
            TypeName = t.TypeName,
            PassCount = t.PassCount,
            FailCount = t.FailCount,
            TotalCount = t.TotalCount,
        }).ToList()
    };
}

public class TemplateResultInfo
{
    public string Type { get; set; } = "";
    public string TypeName { get; set; } = "";
    public int TotalCount { get; set; }
    public int PassCount { get; set; }
    public int FailCount { get; set; }
}
