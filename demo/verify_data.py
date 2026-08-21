#!/usr/bin/env python3
"""
个税数据逻辑验证工具
用法: LD_LIBRARY_PATH=/opt/oracle/instantclient_23_4 .venv/bin/python demo/verify_data.py

功能:
1. 按姓名搜索 → 对比TC93/TC8M/Excel三方数据
2. 按身份证查询 → 明细级对比
3. 批量差异分析
4. 零金额批次检查
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import xlrd
from db import init_db, get_connection
from collections import defaultdict

# ============================================================
# 加载Excel数据
# ============================================================
EXCEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'Material', '202607_税款计算_工资薪金所得（8.17）.xls')

def load_excel():
    """加载Excel, 返回 {身份证: {name, income, pension, medical, unemployed, fund, cum_income, remark, ...}}"""
    wb = xlrd.open_workbook(EXCEL_PATH)
    ws = wb.sheet_by_index(0)
    data = {}
    for row in range(1, ws.nrows):
        id_num = str(ws.cell_value(row, 3)).strip()
        if not id_num:
            continue
        data[id_num] = {
            'row': row,
            'name': str(ws.cell_value(row, 1)).strip(),
            'income': ws.cell_value(row, 7),        # 本期收入
            'pension': ws.cell_value(row, 10),       # 养老
            'medical': ws.cell_value(row, 11),       # 医疗
            'unemployed': ws.cell_value(row, 12),    # 失业
            'fund': ws.cell_value(row, 13),          # 公积金
            'cum_income': ws.cell_value(row, 23) if ws.cell_type(row, 23) != xlrd.XL_CELL_EMPTY else 0,  # 累计收入
            'cum_deduct': ws.cell_value(row, 25) if ws.cell_type(row, 25) != xlrd.XL_CELL_EMPTY else 0,  # 累计专项扣除
            'remark': str(ws.cell_value(row, 49)).strip() if ws.cell_type(row, 49) != xlrd.XL_CELL_EMPTY else '',
            'other_income': ws.cell_value(row, 36) if ws.cell_type(row, 36) != xlrd.XL_CELL_EMPTY else 0,
        }
    return data

# ============================================================
# 数据库查询
# ============================================================
def search_by_name(conn, name):
    """按姓名模糊搜索TC93记录"""
    cur = conn.cursor()
    cur.execute('''
        SELECT AAC002, AAC003, ATB930, ATC931, ATC937,
               NVL(ATC93AA,0) AS gz, NVL(ATC934,0) AS bf1, NVL(ATC935,0) AS bf2, NVL(ATC936,0) AS bf3,
               NVL(ATC93C,0) AS tax_base,
               NVL(BAA001,0) AS pension, NVL(BAA002,0) AS medical, NVL(BAA003,0) AS unemployed, NVL(CAA002,0) AS fund
        FROM TC93
        WHERE AAC003 LIKE :name
        ORDER BY AAC002, ATB930, ATC931, ATC937
    ''', {'name': f'%{name}%'})
    rows = cur.fetchall()
    cur.close()
    return rows

def get_tc93_by_id(conn, id_num):
    """按身份证查TC93全部记录"""
    cur = conn.cursor()
    cur.execute('''
        SELECT AAC003, ATB930, ATC931, ATC937,
               NVL(ATC93AA,0) AS gz, NVL(ATC934,0) AS bf1, NVL(ATC935,0) AS bf2, NVL(ATC936,0) AS bf3,
               NVL(ATC93C,0) AS tax_base,
               NVL(BAA001,0) AS pension, NVL(BAA002,0) AS medical, NVL(BAA003,0) AS unemployed, NVL(CAA002,0) AS fund
        FROM TC93
        WHERE AAC002 = :id
        ORDER BY ATB930, ATC931, ATC937
    ''', {'id': id_num})
    rows = cur.fetchall()
    cur.close()
    return rows

def get_tc8m_by_unit(conn, unit=None, salary_month=None, pay_month=202607):
    """查询TC8M记录"""
    cur = conn.cursor()
    conditions = ['ATC8G7 = :pm']
    bind = {'pm': pay_month}
    if unit:
        conditions.append('ATB930 = :unit')
        bind['unit'] = unit
    if salary_month:
        conditions.append('ATC931 = :sm')
        bind['sm'] = salary_month
    where = ' AND '.join(conditions)
    cur.execute(f'''
        SELECT ATB930, ATC8G7, ATC931, ATC937, ATC8M1, ATC8M2
        FROM TC8M
        WHERE {where}
        ORDER BY ATB930, ATC931, ATC937
    ''', bind)
    rows = cur.fetchall()
    cur.close()
    return rows

def get_tc8m_by_id(conn, id_num, pay_month=202607):
    """按身份证查此人涉及的TC8M记录(通过TC93关联, 限定发放月份)"""
    cur = conn.cursor()
    cur.execute('''
        SELECT DISTINCT m.ATB930, m.ATC8G7, m.ATC931, m.ATC937, m.ATC8M1, m.ATC8M2
        FROM TC8M m
        INNER JOIN TC93 s ON m.ATB930 = s.ATB930 AND m.ATC931 = s.ATC931
        WHERE s.AAC002 = :id AND m.ATC8G7 = :pm
        ORDER BY m.ATB930, m.ATC931, m.ATC937
    ''', {'id': id_num, 'pm': pay_month})
    rows = cur.fetchall()
    cur.close()
    return rows

# ============================================================
# 显示函数
# ============================================================
def print_divider(char='=', width=80):
    print(char * width)

def print_tc93_records(rows, title="TC93记录"):
    """打印TC93记录明细"""
    if not rows:
        print(f"  (无记录)")
        return
    print(f"\n{title} ({len(rows)}条):")
    print(f"  {'姓名':<8} {'结算单元':<8} {'所属月':<8} {'序号':<4} {'工资总额':>10} {'补发1':>8} {'补发2':>8} {'补发3':>8} {'养老':>8} {'医疗':>6} {'失业':>6} {'公积金':>8}")
    total_gz = 0
    total_pension = 0
    total_medical = 0
    total_unemployed = 0
    total_fund = 0
    for r in rows:
        name, unit, month, seq, gz, bf1, bf2, bf3, pension, medical, unemployed, fund = r
        total_gz += gz
        total_pension += pension
        total_medical += medical
        total_unemployed += unemployed
        total_fund += fund
        print(f"  {name:<8} {unit:<8} {month:<8} {seq:<4} {gz:>10.2f} {bf1:>8.2f} {bf2:>8.2f} {bf3:>8.2f} {pension:>8.2f} {medical:>6.2f} {unemployed:>6.2f} {fund:>8.2f}")
    if len(rows) > 1:
        print(f"  {'合计':<8} {'':8} {'':8} {'':4} {total_gz:>10.2f} {'':>8} {'':>8} {'':>8} {total_pension:>8.2f} {total_medical:>6.2f} {total_unemployed:>6.2f} {total_fund:>8.2f}")

def print_tc8m_records(rows, title="TC8M记录"):
    """打印TC8M记录"""
    if not rows:
        print(f"  (无记录)")
        return
    print(f"\n{title} ({len(rows)}条):")
    print(f"  {'结算单元':<8} {'发放月':<8} {'所属月':<8} {'序号':<4} {'人数':>6} {'金额':>12}")
    for r in rows:
        unit, pay_month, salary_month, seq, count, amount = r
        print(f"  {unit:<8} {pay_month:<8} {salary_month:<8} {seq:<4} {count:>6} {(amount or 0):>12.2f}")

def print_comparison(pid, name, excel_data, tc93_summary, tc8m_records):
    """打印三方对比"""
    print_divider()
    print(f"📋 {name} (身份证: {pid})")
    print_divider('-')

    # Excel数据
    if pid in excel_data:
        e = excel_data[pid]
        print(f"\n📊 Excel:")
        print(f"  本期收入: {e['income']:>12.2f}")
        print(f"  养老:     {e['pension']:>12.2f}")
        print(f"  医疗:     {e['medical']:>12.2f}")
        print(f"  失业:     {e['unemployed']:>12.2f}")
        print(f"  公积金:   {e['fund']:>12.2f}")
        if e['cum_income']:
            print(f"  累计收入: {e['cum_income']:>12.2f}")
        if e['other_income']:
            print(f"  其他单位: {e['other_income']:>12.2f}")
        if e['remark']:
            print(f"  备注:     {e['remark']}")
    else:
        print(f"\n📊 Excel: ❌ 不在Excel中")

    # TC93汇总
    if tc93_summary:
        print(f"\n📋 TC93 (ATC8G7=202607批次关联):")
        print(f"  工资总额: {tc93_summary['gz']:>12.2f}")
        print(f"  养老:     {tc93_summary['pension']:>12.2f}")
        print(f"  医疗:     {tc93_summary['medical']:>12.2f}")
        print(f"  失业:     {tc93_summary['unemployed']:>12.2f}")
        print(f"  公积金:   {tc93_summary['fund']:>12.2f}")
        print(f"  记录数:   {tc93_summary['count']}")
        if tc93_summary['months']:
            print(f"  涉及月份: {sorted(tc93_summary['months'])}")
        if tc93_summary['units']:
            print(f"  结算单元: {sorted(tc93_summary['units'])}")
    else:
        print(f"\n📋 TC93: ❌ 无匹配记录")

    # TC8M
    if tc8m_records:
        print(f"\n📦 TC8M:")
        print_tc8m_records(tc8m_records, "")

    # 对比差异
    if pid in excel_data and tc93_summary:
        e = excel_data[pid]
        t = tc93_summary
        print(f"\n🔍 差异分析:")
        inc_diff = e['income'] - t['gz']
        pen_diff = e['pension'] - t['pension']
        med_diff = e['medical'] - t['medical']
        fun_diff = e['fund'] - t['fund']
        print(f"  本期收入: Excel={e['income']:.2f} TC93={t['gz']:.2f} 差={inc_diff:+.2f} {'✅' if abs(inc_diff) < 0.01 else '❌'}")
        print(f"  养老:     Excel={e['pension']:.2f} TC93={t['pension']:.2f} 差={pen_diff:+.2f} {'✅' if abs(pen_diff) < 0.01 else '❌'}")
        print(f"  医疗:     Excel={e['medical']:.2f} TC93={t['medical']:.2f} 差={med_diff:+.2f} {'✅' if abs(med_diff) < 0.01 else '❌'}")
        print(f"  公积金:   Excel={e['fund']:.2f} TC93={t['fund']:.2f} 差={fun_diff:+.2f} {'✅' if abs(fun_diff) < 0.01 else '❌'}")

# ============================================================
# 交互式菜单
# ============================================================
def menu_search_by_name(conn, excel_data):
    name = input("请输入姓名(支持模糊): ").strip()
    if not name:
        return
    rows = search_by_name(conn, name)
    if not rows:
        print(f"未找到包含'{name}'的记录")
        return

    # 按人分组
    by_person = defaultdict(list)
    for r in rows:
        by_person[r[0]].append(r)

    print(f"\n找到 {len(by_person)} 人:")
    for i, (pid, records) in enumerate(by_person.items()):
        rname = records[0][1]
        total_gz = sum(r[5] for r in records)
        in_excel = '✅' if pid in excel_data else '❌'
        excel_inc = excel_data.get(pid, {}).get('income', 0)
        diff = excel_inc - total_gz if pid in excel_data else None
        diff_str = f" 差={diff:+.2f}" if diff is not None else ""
        print(f"  [{i+1}] {rname} ({pid}) TC93={total_gz:.2f} Excel={excel_inc:.2f}{diff_str} {in_excel}")

    choice = input("\n选择编号查看详情 (回车跳过): ").strip()
    if choice and choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(by_person):
            pid = list(by_person.keys())[idx]
            _show_person_detail(conn, pid, excel_data)

def menu_search_by_id(conn, excel_data):
    pid = input("请输入身份证号: ").strip()
    if not pid:
        return
    _show_person_detail(conn, pid, excel_data)

def _show_person_detail(conn, pid, excel_data):
    """显示某人的完整详情"""
    # TC93全部记录
    tc93_rows = get_tc93_by_id(conn, pid)

    # TC8M关联记录
    tc8m_rows = get_tc8m_by_id(conn, pid)

    # 汇总TC93 (仅ATC8G7=202607批次)
    tc8m_units_months = set()
    for r in tc8m_rows:
        tc8m_units_months.add((r[0], r[2]))  # (ATB930, ATC931)

    # r[0]=name r[1]=ATB930 r[2]=ATC931 r[3]=ATC937
    # tc8m_units_months: (ATB930, ATC931) from get_tc8m_by_id
    matched_rows = [r for r in tc93_rows if (r[1], r[2]) in tc8m_units_months or not tc8m_rows]
    # 如果没有TC8M记录，取全部
    if not tc8m_rows:
        matched_rows = tc93_rows

    tc93_summary = None
    if matched_rows:
        gz = sum(r[4] for r in matched_rows)
        pension = sum(r[9] for r in matched_rows)
        medical = sum(r[10] for r in matched_rows)
        unemployed = sum(r[11] for r in matched_rows)
        fund = sum(r[12] for r in matched_rows)
        months = set(r[2] for r in matched_rows)
        units = set(r[1] for r in matched_rows)
        tc93_summary = {
            'gz': gz, 'pension': pension, 'medical': medical,
            'unemployed': unemployed, 'fund': fund,
            'count': len(matched_rows), 'months': months, 'units': units
        }

    name = tc93_rows[0][1] if tc93_rows else excel_data.get(pid, {}).get('name', '?')
    print_comparison(pid, name, excel_data, tc93_summary, tc8m_rows)

    # 额外: 显示全部TC93记录(不限于ATC8G7=202607)
    if tc93_rows and tc8m_rows:
        other_rows = [r for r in tc93_rows if (r[1], r[2]) not in tc8m_units_months]
        if other_rows:
            print(f"\n📌 不在ATC8G7=202607批次的TC93记录 ({len(other_rows)}条):")
            print_tc93_records(other_rows, "")
            other_gz = sum(r[4] for r in other_rows)
            print(f"  这些记录的工资总额: {other_gz:.2f}")

def menu_batch_diff(conn, excel_data):
    """批量差异分析"""
    print("批量差异分析 (TC8M→TC93 vs Excel)")
    limit = input("分析人数 (默认100): ").strip()
    limit = int(limit) if limit.isdigit() else 100

    cur = conn.cursor()
    cur.execute('''
        SELECT s.AAC002, s.AAC003,
               SUM(NVL(s.ATC93AA,0)) AS gz,
               SUM(NVL(s.BAA001,0)) AS pension,
               SUM(NVL(s.BAA002,0)) AS medical,
               SUM(NVL(s.BAA003,0)) AS unemployed,
               SUM(NVL(s.CAA002,0)) AS fund,
               COUNT(*) AS cnt
        FROM TC93 s
        WHERE (s.ATB930, s.ATC931, s.ATC937) IN (
            SELECT DISTINCT ATB930, ATC931, ATC937 FROM TC8M WHERE ATC8G7 = 202607
        )
        GROUP BY s.AAC002, s.AAC003
    ''')
    rows = cur.fetchall()
    cur.close()

    tc_data = {}
    for r in rows:
        if r[0]:
            tc_data[str(r[0]).strip()] = {
                'name': r[1], 'gz': r[2] or 0, 'pension': r[3] or 0,
                'medical': r[4] or 0, 'unemployed': r[5] or 0,
                'fund': r[6] or 0, 'count': r[7]
            }

    diffs = []
    for pid in set(excel_data.keys()) & set(tc_data.keys()):
        e = excel_data[pid]
        t = tc_data[pid]
        inc_diff = e['income'] - t['gz']
        if abs(inc_diff) > 0.01:
            diffs.append((pid, e['name'], e['income'], t['gz'], inc_diff, t['count']))

    diffs.sort(key=lambda x: -abs(x[4]))
    print(f"\n共{len(diffs)}人有差异，显示前{min(limit, len(diffs))}人:")
    print(f"  {'姓名':<8} {'Excel':>12} {'TC93':>12} {'差额':>12} {'记录数':>6}")
    for pid, name, ex, tc, d, cnt in diffs[:limit]:
        print(f"  {name:<8} {ex:>12.2f} {tc:>12.2f} {d:>+12.2f} {cnt:>6}")

    detail = input("\n输入姓名查看详情 (回车跳过): ").strip()
    if detail:
        for pid, name, ex, tc, d, cnt in diffs:
            if detail in name:
                _show_person_detail(conn, pid, excel_data)
                break

def menu_zero_amount(conn):
    """检查零金额批次"""
    cur = conn.cursor()
    cur.execute('''
        SELECT ATB930, ATC931, ATC8M1, ATC8M2
        FROM TC8M
        WHERE ATC8G7 = 202607 AND (ATC8M2 = 0 OR ATC8M2 IS NULL)
        ORDER BY ATB930, ATC931
    ''')
    rows = cur.fetchall()
    cur.close()

    zero_units = defaultdict(list)
    for r in rows:
        zero_units[r[0]].append({'month': r[1], 'count': r[2], 'amount': r[3]})

    print(f"\n零金额批次 (ATC8M2=0): {len(rows)}条, 涉及{len(zero_units)}个单元")
    for unit in sorted(zero_units.keys()):
        records = zero_units[unit]
        total_people = sum(r['count'] for r in records)
        print(f"  单元{unit}: {len(records)}批/{total_people}人")

def menu_stats(excel_data, conn):
    """总体统计"""
    cur = conn.cursor()

    # TC8M→TC93人数 (IN子查询)
    cur.execute('''
        SELECT COUNT(DISTINCT s.AAC002)
        FROM TC93 s
        WHERE (s.ATB930, s.ATC931, s.ATC937) IN (
            SELECT DISTINCT ATB930, ATC931, ATC937 FROM TC8M WHERE ATC8G7 = 202607
        )
    ''')
    tc_count = cur.fetchone()[0]

    # 共同/仅Excel/仅TC
    common = set(excel_data.keys())
    cur.execute('''
        SELECT s.AAC002
        FROM TC93 s
        WHERE (s.ATB930, s.ATC931, s.ATC937) IN (
            SELECT DISTINCT ATB930, ATC931, ATC937 FROM TC8M WHERE ATC8G7 = 202607
        )
        GROUP BY s.AAC002
    ''')
    tc_ids = set(str(r[0]).strip() for r in cur.fetchall() if r[0])
    cur.close()

    common_ids = common & tc_ids
    only_excel = common - tc_ids
    only_tc = tc_ids - common

    print(f"\n{'='*60}")
    print(f"📊 总体统计")
    print(f"{'='*60}")
    print(f"  Excel人数:        {len(excel_data):>8}")
    print(f"  TC8M→TC93人数:    {tc_count:>8}")
    print(f"  共同:             {len(common_ids):>8}")
    print(f"  仅Excel:          {len(only_excel):>8}")
    print(f"  仅TC8M→TC93:      {len(only_tc):>8}")

    # 收入匹配
    exact = 0
    for pid in common_ids:
        if abs(excel_data[pid]['income'] - 0) < 0.01:
            continue  # skip zeros
        # need tc data
    print(f"\n  (运行批量差异分析查看详细匹配情况)")

# ============================================================
# 主程序
# ============================================================
def main():
    print("=" * 60)
    print("  个税数据逻辑验证工具")
    print("  Excel: 202607_税款计算_工资薪金所得（8.17）.xls")
    print("=" * 60)

    print("\n加载Excel...")
    excel_data = load_excel()
    print(f"  Excel加载完成: {len(excel_data)}人")

    print("连接数据库...")
    init_db()
    conn = get_connection()
    print("  数据库连接成功")

    while True:
        print(f"\n{'='*40}")
        print("菜单:")
        print("  1. 按姓名搜索")
        print("  2. 按身份证查询")
        print("  3. 批量差异分析")
        print("  4. 零金额批次检查")
        print("  5. 总体统计")
        print("  0. 退出")
        print(f"{'='*40}")

        choice = input("选择: ").strip()
        if choice == '1':
            menu_search_by_name(conn, excel_data)
        elif choice == '2':
            menu_search_by_id(conn, excel_data)
        elif choice == '3':
            menu_batch_diff(conn, excel_data)
        elif choice == '4':
            menu_zero_amount(conn)
        elif choice == '5':
            menu_stats(excel_data, conn)
        elif choice == '0':
            break
        else:
            print("无效选择")

    conn.close()
    print("\n再见!")

if __name__ == '__main__':
    main()
