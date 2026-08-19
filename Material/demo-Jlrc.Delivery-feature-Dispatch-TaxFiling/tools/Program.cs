using System.Text;
using NPOI.HSSF.UserModel;
using NPOI.SS.UserModel;

Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);

void PrintSheet(string path) {
    using var fs = File.OpenRead(path);
    var wb = new HSSFWorkbook(fs);
    var sheet = wb.GetSheetAt(0);
    Console.WriteLine($"=== {Path.GetFileName(path)} === Rows: {sheet.LastRowNum+1}");
    for (int r = 0; r <= Math.Min(sheet.LastRowNum, 25); r++) {
        var row = sheet.GetRow(r);
        if (row == null) { Console.WriteLine($"Row {r}: <null>"); continue; }
        var cells = new List<string>();
        for (int c = 0; c < Math.Min((int)row.LastCellNum, 50); c++) {
            var v = row.GetCell(c)?.ToString()?.Trim() ?? "";
            if (!string.IsNullOrEmpty(v))
                cells.Add($"[{c}]={v}");
        }
        if (cells.Count > 0)
            Console.WriteLine($"Row {r}: {string.Join(" | ", cells)}");
        else
            Console.WriteLine($"Row {r}: <empty>");
    }
}

PrintSheet(@"d:\MyDevelop\jlrc\Jlrc.Delivery\Dispatch-TaxFiling\stuff\工资发放1.xls");
Console.WriteLine("\n");
PrintSheet(@"d:\MyDevelop\jlrc\Jlrc.Delivery\Dispatch-TaxFiling\stuff\工资发放2.xls");
