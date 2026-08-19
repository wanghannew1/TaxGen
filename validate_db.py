import oracledb
from db import init_db, get_connection

def explore_database():
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    
    print("=" * 60)
    print("1. Oracle Version")
    print("=" * 60)
    cursor.execute("SELECT * FROM v$version")
    print(cursor.fetchone())
    
    print("\n" + "=" * 60)
    print("2. TC94 AAA901 Code Values (Insurance Types)")
    print("=" * 60)
    cursor.execute("""
        SELECT DISTINCT AAA901, AAA902 
        FROM TC94 
        WHERE ROWNUM <= 100
    """)
    for row in cursor.fetchall():
        print(f"  AAA901={row[0]}, AAA902={row[1]}")
    
    print("\n" + "=" * 60)
    print("3. TC93 Sample Data")
    print("=" * 60)
    cursor.execute("""
        SELECT * FROM TC93 
        WHERE ATC931 = 202607 
        AND ROWNUM <= 5
    """)
    columns = [desc[0] for desc in cursor.description]
    print(f"Columns: {columns}")
    for row in cursor.fetchall():
        print(f"  {row}")
    
    print("\n" + "=" * 60)
    print("4. TC94 Deduction Details (for first TC93 record)")
    print("=" * 60)
    cursor.execute("""
        SELECT ATC930 FROM TC93 
        WHERE ATC931 = 202607 AND ROWNUM = 1
    """)
    atc930 = cursor.fetchone()[0]
    print(f"ATC930: {atc930}")
    
    cursor.execute("""
        SELECT ATC930, AAA901, ATC941 
        FROM TC94 
        WHERE ATC930 = :id
    """, [atc930])
    for row in cursor.fetchall():
        print(f"  ATC930={row[0]}, AAA901={row[1]}, ATC941={row[2]}")
    
    print("\n" + "=" * 60)
    print("5. Search for 大病险/补缴 columns")
    print("=" * 60)
    cursor.execute("""
        SELECT column_name FROM all_tab_columns 
        WHERE column_name LIKE '%大病%' 
        OR column_name LIKE '%补缴%'
        OR column_name LIKE '%DABING%'
        OR column_name LIKE '%BUJIAO%'
    """)
    results = cursor.fetchall()
    if results:
        for row in results:
            print(f"  Found: {row[0]}")
    else:
        print("  No columns found with 大病/补缴 in name")
    
    print("\n" + "=" * 60)
    print("6. TC93 ATC93 columns")
    print("=" * 60)
    cursor.execute("""
        SELECT column_name, data_type 
        FROM all_tab_columns 
        WHERE table_name = 'TC93' 
        AND column_name LIKE '%ATC93%'
    """)
    for row in cursor.fetchall():
        print(f"  {row[0]} ({row[1]})")
    
    print("\n" + "=" * 60)
    print("7. AAA901 codes for insurance types")
    print("=" * 60)
    cursor.execute("""
        SELECT DISTINCT AAA901, AAA902 
        FROM TC94 
        WHERE AAA902 LIKE '%养老%' 
        OR AAA902 LIKE '%医疗%'
        OR AAA902 LIKE '%失业%'
        OR AAA902 LIKE '%公积%'
    """)
    for row in cursor.fetchall():
        print(f"  AAA901={row[0]}, AAA902={row[1]}")
    
    conn.close()
    print("\n" + "=" * 60)
    print("Exploration Complete")
    print("=" * 60)

if __name__ == "__main__":
    explore_database()
