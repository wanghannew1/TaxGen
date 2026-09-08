"""import_filing_history.py - 批量导入历史申报 .xls 文件到 filing_record 表

扫描 /home/ubuntu/excel_example/taxgen/his/*.xls，逐个解析并 upsert 入库。
可重复运行（幂等，upsert 语义，不清空已有数据）。
"""
import glob
import os

from filing_history import init_db, parse_filing_file, import_filing_records

HIS_DIR = "/home/ubuntu/excel_example/taxgen/his"


def main():
    init_db()
    files = sorted(glob.glob(os.path.join(HIS_DIR, "*.xls")))
    total = 0
    for fp in files:
        try:
            records = parse_filing_file(fp)
        except Exception as e:
            print(f"[跳过] {os.path.basename(fp)}: {e}")
            continue
        n = import_filing_records(records)
        total += n
        print(f"{os.path.basename(fp)}: {n} 行")
    print(f"\n共导入 {len(files)} 个文件，{total} 行")


if __name__ == "__main__":
    main()
