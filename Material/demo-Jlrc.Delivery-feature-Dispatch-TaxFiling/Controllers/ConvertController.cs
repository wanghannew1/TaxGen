using Microsoft.AspNetCore.Mvc;
using TaxFilingService.Models;
using TaxFilingService.Services;

namespace TaxFilingService.Controllers;

[ApiController]
[Route("api/[controller]")]
public class ConvertController : ControllerBase
{
    private readonly SourceParser _parser;
    private readonly TemplateFiller _templateFiller;
    private readonly TaskManagerService _taskManager;
    private readonly ILogger<ConvertController> _logger;

    public ConvertController(
        SourceParser parser,
        TemplateFiller templateFiller,
        TaskManagerService taskManager,
        ILogger<ConvertController> logger)
    {
        _parser = parser;
        _templateFiller = templateFiller;
        _taskManager = taskManager;
        _logger = logger;
    }

    [HttpPost("preview")]
    [RequestSizeLimit(536870912)]
    public async Task<IActionResult> Preview(IFormFile file)
    {
        if (file == null || file.Length == 0)
            return BadRequest(new { message = "请上传数据源文件" });

        using var stream = new MemoryStream();
        await file.CopyToAsync(stream);
        stream.Position = 0;

        var result = _parser.Parse(stream, file.FileName);
        if (result.Error != null)
            return BadRequest(new { message = result.Error });

        var records = result.Records.Select(r => new
        {
            姓名 = r.姓名,
            身份证 = r.身份证,
            职工号 = r.职工号,
            应发工资 = r.应发工资,
            实发工资 = r.实发工资,
            采暖费 = r.采暖费,
            奖金 = r.奖金,
            养老个人 = r.养老个人,
            失业个人 = r.失业个人,
            医疗个人 = r.医疗个人,
            公积金个人 = r.公积金个人,
            个人所得税 = r.个人所得税,
            扣款合计 = r.扣款合计,
            补缴及退款保险金额个人 = r.补缴及退款保险金额个人,
            大病险个人 = r.大病险个人,
            独生子女费 = r.独生子女费,
            本期收入 = TemplateFiller.Calc本期收入(r),
            免税 = TemplateFiller.Calc免税(r),
            validation = TemplateFiller.GetValidationText(r)
        }).ToList();

        return Ok(new
        {
            title = result.Title,
            total = result.Records.Count,
            records,
            logs = result.Logs
        });
    }

    [HttpPost("start")]
    [RequestSizeLimit(536870912)]
    public async Task<IActionResult> StartConvert(
        IFormFile file,
        [FromForm] List<string> templates)
    {
        if (file == null || file.Length == 0)
            return BadRequest(new { message = "请上传数据源文件" });

        if (templates == null || templates.Count == 0)
            return BadRequest(new { message = "请选择至少一个导出类型" });

        var taskId = _taskManager.CreateTask();
        var taskDir = _taskManager.GetTaskDir(taskId);

        using var stream = new MemoryStream();
        await file.CopyToAsync(stream);
        stream.Position = 0;

        var parseResult = _parser.Parse(stream, file.FileName);
        if (parseResult.Error != null)
        {
            _taskManager.FailTask(taskId, parseResult.Error);
            return BadRequest(new { message = parseResult.Error });
        }

        _ = Task.Run(async () =>
        {
            try
            {
                var progress = new ConvertProgress { Status = "processing" };
                progress.Logs.AddRange(parseResult.Logs);
                var total = templates.Count;
                var completed = 0;
                var resultPaths = new List<string>();

                foreach (var tpl in templates)
                {
                    completed++;
                    progress.Percent = (completed - 1) * 100 / total;
                    progress.Message = $"正在生成: {GetTemplateName(tpl)} ({completed}/{total})";
                    _taskManager.UpdateProgress(taskId, progress);

                    progress.Logs.Clear();
                    progress.TemplateResults.Clear();
                    ConvertResult convertResult;
                    switch (tpl)
                    {
                        case "normalSalary":
                            convertResult = await _templateFiller.FillNormalSalaryAsync(
                                parseResult.Records, parseResult.Title, taskDir);
                            break;
                        case "laborService":
                            convertResult = await _templateFiller.FillLaborServiceAsync(
                                parseResult.Records, parseResult.Title, taskDir);
                            break;
                        case "annualBonus":
                            convertResult = await _templateFiller.FillAnnualBonusAsync(
                                parseResult.Records, parseResult.Title, taskDir);
                            break;
                        case "personnelInfo":
                            convertResult = await _templateFiller.FillPersonnelInfoAsync(
                                parseResult.Records, parseResult.Title, taskDir);
                            break;
                        default:
                            continue;
                    }
                    progress.Logs.AddRange(convertResult.Logs);
                    if (convertResult.Validations.Count > 0)
                    {
                        var passCount = convertResult.Validations.Count(v => v.通过);
                        var failCount = convertResult.Validations.Count - passCount;
                        progress.TemplateResults.Add(new TemplateResultInfo
                        {
                            Type = tpl,
                            TypeName = GetTemplateName(tpl),
                            TotalCount = convertResult.Validations.Count,
                            PassCount = passCount,
                            FailCount = failCount,
                        });
                    }
                    resultPaths.Add(convertResult.OutputPath);
                    _taskManager.UpdateProgress(taskId, progress);
                }

                progress.Status = "completed";
                progress.Percent = 100;
                progress.Message = $"转换完成，共生成 {resultPaths.Count} 个文件";
                progress.ResultPaths = resultPaths;
                _taskManager.UpdateProgress(taskId, progress);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "任务 {TaskId} 失败", taskId);
                _taskManager.FailTask(taskId, ex.Message);
            }
        });

        return Ok(new { taskId });
    }

    [HttpGet("progress/{taskId}")]
    public async Task Progress(string taskId)
    {
        var progress = _taskManager.GetProgress(taskId);
        if (progress == null)
        {
            Response.StatusCode = 404;
            return;
        }

        Response.ContentType = "text/event-stream";
        Response.Headers["Cache-Control"] = "no-cache";
        Response.Headers["Connection"] = "keep-alive";

        var cts = HttpContext.RequestAborted;

        async Task Send(string json)
        {
            try
            {
                await Response.Body.WriteAsync(
                    System.Text.Encoding.UTF8.GetBytes($"data: {json}\n\n"), cts);
                await Response.Body.FlushAsync(cts);
            }
            catch { }
        }

        try
        {
            await Send(System.Text.Json.JsonSerializer.Serialize(progress));

            while (!cts.IsCancellationRequested)
            {
                await Task.Delay(500, cts);
                var p = _taskManager.GetProgress(taskId);
                if (p == null) break;
                await Send(System.Text.Json.JsonSerializer.Serialize(p));
                if (p.Status is "completed" or "failed")
                    break;
            }
        }
        catch (OperationCanceledException) { }
    }

    [HttpGet("download/{taskId}")]
    public IActionResult Download(string taskId, [FromQuery] int index = 0)
    {
        var progress = _taskManager.GetProgress(taskId);
        if (progress == null)
            return NotFound(new { message = "任务不存在" });
        if (progress.Status != "completed")
            return BadRequest(new { message = "任务尚未完成" });
        if (index < 0 || index >= progress.ResultPaths.Count)
            return BadRequest(new { message = "文件索引无效" });

        var path = progress.ResultPaths[index];
        if (!System.IO.File.Exists(path))
            return NotFound(new { message = "结果文件不存在" });

        var fileName = Path.GetFileName(path);
        return PhysicalFile(path,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            fileName);
    }

    [HttpGet("download-all/{taskId}")]
    public IActionResult DownloadAll(string taskId)
    {
        var progress = _taskManager.GetProgress(taskId);
        if (progress == null)
            return NotFound(new { message = "任务不存在" });
        if (progress.Status != "completed")
            return BadRequest(new { message = "任务尚未完成" });

        var files = progress.ResultPaths.Where(f => System.IO.File.Exists(f)).ToList();
        if (files.Count == 0)
            return NotFound(new { message = "没有可下载的文件" });

        using var zipStream = new MemoryStream();
        using (var zip = new System.IO.Compression.ZipArchive(zipStream, System.IO.Compression.ZipArchiveMode.Create, true))
        {
            foreach (var file in files)
            {
                var entry = zip.CreateEntry(Path.GetFileName(file));
                using var entryStream = entry.Open();
                using var fileStream = new FileStream(file, FileMode.Open, FileAccess.Read);
                fileStream.CopyTo(entryStream);
            }
        }

        zipStream.Position = 0;
        return File(zipStream.ToArray(), "application/zip", $"TaxFiling_{DateTime.Now:yyyyMMddHHmmss}.zip");
    }

    [HttpGet("status/{taskId}")]
    public IActionResult Status(string taskId)
    {
        var p = _taskManager.GetProgress(taskId);
        if (p == null) return NotFound(new { message = "任务不存在" });
        return Ok(p);
    }

    private static string GetTemplateName(string tpl) => tpl switch
    {
        "normalSalary" => "正常工资薪金所得",
        "laborService" => "劳务报酬所得",
        "annualBonus" => "全年一次性奖金收入",
        "personnelInfo" => "人员信息采集导入模板",
        _ => tpl
    };
}
