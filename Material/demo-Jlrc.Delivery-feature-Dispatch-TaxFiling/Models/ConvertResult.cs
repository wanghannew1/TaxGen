namespace TaxFilingService.Models;

public class ConvertResult
{
    public string OutputPath { get; set; } = "";
    public string TemplateType { get; set; } = "";
    public List<LogEntry> Logs { get; set; } = new();
    public List<ValidationDetail> Validations { get; set; } = new();
}

public class ValidationDetail
{
    public string 姓名 { get; set; } = "";
    public decimal 本期收入 { get; set; }
    public decimal 左 { get; set; }
    public decimal 右 { get; set; }
    public decimal 差值 { get; set; }
    public bool 通过 { get; set; }
}
