"""Excel 说明 sheet 生成器。

在生成的每个 Excel 文件中追加一个"说明" sheet, 对该文件内所有 sheet 的生成规则
进行详细说明, 便于新用户理解各 sheet 的数据来源与判定逻辑。

各模板生成器只需调用 add_explanation_sheet(wb, sections), 传入每个 sheet 的
说明块即可。共享一份渲染逻辑, 保证所有模板的说明 sheet 风格一致。
"""
from typing import List, Sequence, Tuple


def add_explanation_sheet(wb, sections: Sequence[Tuple[str, Sequence[str]]]) -> None:
    """向工作簿追加"说明" sheet。

    Args:
        wb: openpyxl Workbook
        sections: 每个元组为 (sheet 名称, 说明行列表)。说明行直接写入,
            块标题加粗。各 sheet 之间以空行分隔。
    """
    ws = wb.create_sheet("说明")
    ws.column_dimensions["A"].width = 110

    def w(row, col, text, bold=False):
        cell = ws.cell(row=row, column=col, value=text)
        if isinstance(text, str) and text.startswith("="):
            cell.data_type = "s"
        if bold:
            cell.font = cell.font.copy(bold=True)
        return cell

    w(1, 1, "本文件生成规则说明", bold=True)
    w(2, 1, "以下逐个说明本文件中各 sheet 的数据来源与生成规则。")

    row = 4
    for sheet_name, lines in sections:
        w(row, 1, f"■ {sheet_name}", bold=True)
        row += 1
        for line in lines:
            w(row, 1, line)
            row += 1
        row += 1  # 各 sheet 之间空一行

    return ws
