"""端到端集成测试"""
import os
import pytest
from openpyxl import load_workbook
from db import get_connection
from queries import get_available_months, get_salary_records, get_deduction_details, get_personnel_info
from models import MonthOption, SalaryRecord, PersonnelInfo
from templates_gen.normal_salary import generate_normal_salary
from templates_gen.labor_service import generate_labor_service
from templates_gen.annual_bonus import generate_annual_bonus
from templates_gen.personnel_info import generate_personnel_info
from templates_gen.validation import validate_salary_records


class TestQueryFunctions:
    """测试查询函数"""
    
    def test_get_available_months(self, conn):
        """测试月份查询"""
        months = get_available_months(conn)
        assert len(months) > 0
        assert isinstance(months[0], MonthOption)
        assert months[0].value > 202000  # 应该是合理的年月格式
    
    def test_get_salary_records(self, conn):
        """测试工资记录查询"""
        # 使用最近的月份
        months = get_available_months(conn)
        latest_month = months[0].value
        records = get_salary_records(conn, latest_month)
        assert len(records) > 0
        assert isinstance(records[0], SalaryRecord)
        assert records[0].姓名 is not None
        assert records[0].职工号 is not None
    
    def test_get_salary_records_fields(self, conn):
        """测试工资记录字段完整性"""
        months = get_available_months(conn)
        latest_month = months[0].value
        records = get_salary_records(conn, latest_month)
        rec = records[0]
        # 关键字段应该存在
        assert hasattr(rec, '应发工资')
        assert hasattr(rec, '养老个人')
        assert hasattr(rec, '医疗个人')
        assert hasattr(rec, '失业个人')
        assert hasattr(rec, '公积金个人')
        assert hasattr(rec, '实发工资')
        assert hasattr(rec, '个人所得税')
        assert hasattr(rec, '补缴及退款保险金额个人')
        assert hasattr(rec, '大病险个人')
        assert hasattr(rec, '采暖费')
        assert hasattr(rec, '独生子女费')
    
    def test_get_personnel_info(self, conn):
        """测试人员信息查询"""
        months = get_available_months(conn)
        latest_month = months[0].value
        personnel = get_personnel_info(conn, latest_month)
        assert len(personnel) > 0
        assert isinstance(personnel[0], PersonnelInfo)
        assert personnel[0].姓名 is not None


class TestTemplateGeneration:
    """测试模板生成"""
    
    def test_generate_normal_salary(self, conn, output_dir):
        """测试正常工资薪金所得模板生成"""
        months = get_available_months(conn)
        latest_month = months[0].value
        records = get_salary_records(conn, latest_month)
        result = generate_normal_salary(records, f"测试工资{latest_month}", output_dir)
        assert result.file_path is not None
        assert os.path.exists(result.file_path)
        assert result.record_count == len(records)
        assert result.template_type == "正常工资薪金所得"
        # 验证 Excel 内容
        wb = load_workbook(result.file_path)
        ws = wb.active
        assert ws.max_row == len(records) + 1  # 表头 + 数据行
        assert ws.max_column == 29  # 29 列
    
    def test_generate_labor_service(self, conn, output_dir):
        """测试劳务报酬所得模板生成"""
        months = get_available_months(conn)
        latest_month = months[0].value
        records = get_salary_records(conn, latest_month)
        result = generate_labor_service(records, f"测试劳务{latest_month}", output_dir)
        assert result.file_path is not None
        assert os.path.exists(result.file_path)
        assert result.record_count == len(records)
        assert result.template_type == "劳务报酬所得"
        wb = load_workbook(result.file_path)
        ws = wb.active
        assert ws.max_row == len(records) + 1
        assert ws.max_column == 14
    
    def test_generate_annual_bonus(self, conn, output_dir):
        """测试全年一次性奖金收入模板生成"""
        months = get_available_months(conn)
        latest_month = months[0].value
        records = get_salary_records(conn, latest_month)
        result = generate_annual_bonus(records, f"测试奖金{latest_month}", output_dir)
        assert result.file_path is not None
        assert os.path.exists(result.file_path)
        assert result.template_type == "全年一次性奖金收入"
        wb = load_workbook(result.file_path)
        ws = wb.active
        assert ws.max_column == 11
    
    def test_generate_personnel_info(self, conn, output_dir):
        """测试人员信息采集导入模板生成"""
        months = get_available_months(conn)
        latest_month = months[0].value
        personnel = get_personnel_info(conn, latest_month)
        result = generate_personnel_info(personnel, f"测试人员{latest_month}", output_dir)
        assert result.file_path is not None
        assert os.path.exists(result.file_path)
        assert result.record_count == len(personnel)
        assert result.template_type == "人员信息采集导入模板"
        wb = load_workbook(result.file_path)
        ws = wb.active
        assert ws.max_column == 51


class TestValidation:
    """测试验证功能"""
    
    def test_validate_salary_records(self, conn):
        """测试工资记录验证"""
        months = get_available_months(conn)
        latest_month = months[0].value
        records = get_salary_records(conn, latest_month)
        report = validate_salary_records(records)
        assert report.total_count == len(records)
        assert report.pass_count + report.fail_count == report.total_count
        assert report.pass_count >= 0
        assert report.fail_count >= 0
    
    def test_validation_report_structure(self, conn):
        """测试验证报告结构"""
        months = get_available_months(conn)
        latest_month = months[0].value
        records = get_salary_records(conn, latest_month)
        report = validate_salary_records(records)
        # 如果有失败记录，检查详情结构
        if report.fail_count > 0:
            detail = report.details[0]
            assert '姓名' in detail
            assert '职工号' in detail
            assert '左' in detail
            assert '右' in detail
            assert '差值' in detail


class TestFlaskAPI:
    """测试 Flask API"""
    
    def test_months_api(self, app_client):
        """测试月份 API"""
        resp = app_client.get('/api/months')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) > 0
        assert 'value' in data[0]
        assert 'label' in data[0]
    
    def test_generate_api(self, app_client):
        """测试生成 API"""
        resp = app_client.post('/api/generate', 
            json={"month": 202607, "templates": ["normalSalary"]})
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'files' in data
        assert len(data['files']) > 0
        assert 'download_url' in data['files'][0]
    
    def test_validate_api(self, app_client):
        """测试验证 API"""
        resp = app_client.get('/api/validate/202607')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'total_count' in data
        assert 'pass_count' in data
        assert 'fail_count' in data
    
    def test_download_api(self, app_client):
        """测试下载 API"""
        # 先生成文件
        resp = app_client.post('/api/generate', 
            json={"month": 202607, "templates": ["normalSalary"]})
        data = resp.get_json()
        filename = data['files'][0]['name']
        # 下载文件
        resp = app_client.get(f'/api/download/{filename}')
        assert resp.status_code == 200
        assert resp.content_type == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    
    def test_error_handling(self, app_client):
        """测试错误处理"""
        # 无效月份
        resp = app_client.post('/api/generate', 
            json={"month": 999999, "templates": ["normalSalary"]})
        assert resp.status_code == 400
        
        # 无效模板
        resp = app_client.post('/api/generate', 
            json={"month": 202607, "templates": ["invalidTemplate"]})
        assert resp.status_code == 200  # 无有效模板，返回空文件列表


class TestEdgeCases:
    """测试边缘情况"""
    
    def test_empty_month(self, app_client):
        """测试空月份"""
        resp = app_client.post('/api/generate', 
            json={"month": None, "templates": ["normalSalary"]})
        assert resp.status_code == 400
    
    def test_no_templates(self, app_client):
        """测试未选择模板"""
        resp = app_client.post('/api/generate', 
            json={"month": 202607, "templates": []})
        assert resp.status_code == 400
    
    def test_missing_file_download(self, app_client):
        """测试下载不存在的文件"""
        resp = app_client.get('/api/download/nonexistent.xlsx')
        assert resp.status_code == 404
