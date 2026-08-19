using System.Collections.Concurrent;
using TaxFilingService.Models;

namespace TaxFilingService.Services;

public class TaskManagerService
{
    private readonly ConcurrentDictionary<string, ConvertProgress> _tasks = new();
    private readonly string _baseDir;

    public TaskManagerService(IWebHostEnvironment env)
    {
        _baseDir = Path.Combine(env.ContentRootPath, "App_Data", "Tasks");
        Directory.CreateDirectory(_baseDir);
    }

    public string CreateTask()
    {
        var taskId = Guid.NewGuid().ToString("N");
        var progress = new ConvertProgress
        {
            TaskId = taskId,
            Status = "pending",
            Percent = 0,
            Message = "任务已创建"
        };
        _tasks[taskId] = progress;
        return taskId;
    }

    public string GetTaskDir(string taskId)
    {
        var dir = Path.Combine(_baseDir, taskId);
        Directory.CreateDirectory(dir);
        return dir;
    }

    public ConvertProgress? GetProgress(string taskId)
    {
        return _tasks.TryGetValue(taskId, out var p) ? p.Clone() : null;
    }

    public void UpdateProgress(string taskId, ConvertProgress progress)
    {
        if (_tasks.TryGetValue(taskId, out var existing))
        {
            existing.Status = progress.Status;
            existing.Percent = progress.Percent;
            existing.Message = progress.Message;
            if (!string.IsNullOrEmpty(progress.Error))
                existing.Error = progress.Error;
            if (progress.ResultPaths.Count > 0)
                existing.ResultPaths = new List<string>(progress.ResultPaths);
            if (progress.Logs.Count > 0)
                existing.Logs.AddRange(progress.Logs);
            if (progress.TemplateResults.Count > 0)
                existing.TemplateResults.AddRange(progress.TemplateResults);
        }
    }

    public void FailTask(string taskId, string error)
    {
        if (_tasks.TryGetValue(taskId, out var p))
        {
            p.Status = "failed";
            p.Error = error;
        }
    }
}
