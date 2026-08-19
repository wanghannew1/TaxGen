namespace TaxFilingService.Models;

public class LogEntry
{
    public string Stage { get; set; } = "";
    public string Type { get; set; } = "info";
    public string Message { get; set; } = "";
}
