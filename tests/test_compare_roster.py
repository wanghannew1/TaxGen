"""compare_roster 纯函数单元测试 (无需 Oracle 连接)

覆盖 4 个纯函数:
- map_tax_roster_to_export: roster 行 → 17 键导出格式 (与 parse_tax_export 键一致)
- validate_choices: choices JSON 字符串校验/归一
- apply_compare_choices: 排除/默认确认/分组排除映射
- build_candidate_payload: 弹窗候选 JSON 组装 (reason 推导/unit 映射/counts)
"""
import json

import pytest

from templates_gen.compare_roster import (
    apply_compare_choices,
    build_candidate_payload,
    map_tax_roster_to_export,
    validate_choices,
)

# 与 tax_export_parser.parse_tax_export() 返回的 17 键完全一致
EXPORT_KEYS = [
    "工号", "姓名", "证件类型", "证件号码", "性别", "出生日期", "报送状态",
    "身份验证状态", "手机号码", "任职受雇从业日期", "离职日期", "是否扣除减除费用",
    "任职受雇从业类型", "其他情况说明", "国籍", "备注", "更新时间",
]
# 映射中留空的键 (roster 无对应字段)
EMPTY_KEYS = ["报送状态", "身份验证状态", "手机号码", "是否扣除减除费用",
              "其他情况说明", "备注", "更新时间"]


class TestMapTaxRosterToExport:
    """roster 行 → 17 键导出格式映射"""

    def test_domestic_id_card_row_full_keys_and_derived(self):
        row = {
            "cert_no": "110105199001011234",
            "name": "测试",
            "emp_no": "E001",
            "cert_type": "居民身份证",
            "hire_date": "2020-01-01",
            "leave_date": "",
            "nationality": "",
            "source": "境内",
            "imported_at": "2026-09-01 10:00:00",
        }
        d = map_tax_roster_to_export(row)
        assert set(d.keys()) == set(EXPORT_KEYS)
        assert d["工号"] == "E001"
        assert d["姓名"] == "测试"
        assert d["证件类型"] == "居民身份证"
        assert d["证件号码"] == "110105199001011234"
        assert d["任职受雇从业日期"] == "2020-01-01"
        assert d["离职日期"] == ""
        assert d["任职受雇从业类型"] == "雇员"
        assert d["国籍"] == "中国"  # 空国籍 → 默认中国
        assert d["性别"] == "男"    # 身份证第 17 位 3 为奇数
        assert d["出生日期"] == "19900101"
        for k in EMPTY_KEYS:
            assert d[k] == ""
        # 全部 str, 无 None, 无嵌套
        assert all(isinstance(v, str) for v in d.values())
        assert all(not isinstance(v, (dict, list)) for v in d.values())

    def test_passport_row_gender_birth_empty(self):
        row = {
            "cert_no": "P12345678",
            "name": "外籍",
            "emp_no": "",
            "cert_type": "护照",
            "hire_date": "",
            "leave_date": "",
            "nationality": "",
            "source": "境外",
            "imported_at": "",
        }
        d = map_tax_roster_to_export(row)
        assert set(d.keys()) == set(EXPORT_KEYS)
        assert d["性别"] == ""       # 非身份证 → 不推导
        assert d["出生日期"] == ""
        assert d["任职受雇从业类型"] == "雇员"
        assert d["国籍"] == "中国"
        assert all(isinstance(v, str) for v in d.values())

    def test_missing_keys_default_empty(self):
        # 仅 cert_no/name, 其余键缺失 → 一律空串兜底
        d = map_tax_roster_to_export({"cert_no": "110105199001011234", "name": "张三"})
        assert set(d.keys()) == set(EXPORT_KEYS)
        assert d["工号"] == ""
        assert d["证件类型"] == ""
        assert d["任职受雇从业日期"] == ""
        assert d["离职日期"] == ""
        assert d["国籍"] == "中国"
        # cert_type 缺失 (非身份证) → 性别/出生日期留空
        assert d["性别"] == ""
        assert d["出生日期"] == ""
        assert all(isinstance(v, str) for v in d.values())

    def test_invalid_id_derivation_fails_empty(self):
        # 身份证号长度不足 → 推导失败留空
        d = map_tax_roster_to_export({
            "cert_no": "1101051990",
            "name": "短号",
            "cert_type": "居民身份证",
        })
        assert d["性别"] == ""
        assert d["出生日期"] == ""


class TestValidateChoices:
    """choices JSON 字符串校验/归一"""

    def test_valid_array_normalized(self):
        raw = json.dumps([
            {"group": "add", "cert_no": " 110105199001011234x ", "action": "confirm"},
            {"group": "departed", "cert_no": "P12345678", "action": "exclude"},
            {"group": "pending", "cert_no": "b002", "action": "confirm"},
        ])
        assert validate_choices(raw) == [
            {"group": "add", "cert_no": "110105199001011234X", "action": "confirm"},
            {"group": "departed", "cert_no": "P12345678", "action": "exclude"},
            {"group": "pending", "cert_no": "B002", "action": "confirm"},
        ]

    def test_non_json_raises(self):
        with pytest.raises(ValueError):
            validate_choices("not-json")

    def test_not_array_raises(self):
        with pytest.raises(ValueError):
            validate_choices('{"group": "add"}')

    def test_non_string_input_raises(self):
        with pytest.raises(ValueError):
            validate_choices(None)
        with pytest.raises(ValueError):
            validate_choices(["not", "a", "string"])

    def test_invalid_group_raises(self):
        with pytest.raises(ValueError):
            validate_choices('[{"group": "x", "cert_no": "A", "action": "confirm"}]')

    def test_invalid_action_raises(self):
        with pytest.raises(ValueError):
            validate_choices('[{"group": "add", "cert_no": "A", "action": "skip"}]')

    def test_missing_cert_no_raises(self):
        with pytest.raises(ValueError):
            validate_choices('[{"group": "add", "action": "confirm"}]')

    def test_empty_cert_no_raises(self):
        with pytest.raises(ValueError):
            validate_choices('[{"group": "add", "cert_no": "  ", "action": "confirm"}]')

    def test_non_string_cert_no_raises(self):
        with pytest.raises(ValueError):
            validate_choices('[{"group": "add", "cert_no": 123, "action": "confirm"}]')

    def test_invalid_item_at_tail_rejected(self):
        # 非法项在列表尾部也必须被拒绝, 而非截断
        with pytest.raises(ValueError):
            validate_choices(
                '[{"group": "add", "cert_no": "A", "action": "confirm"},'
                ' {"group": "bad", "cert_no": "B", "action": "confirm"}]')

    def test_non_dict_item_raises(self):
        with pytest.raises(ValueError):
            validate_choices('["just-a-string"]')


class TestApplyCompareChoices:
    """choices 应用: 排除/默认确认/分组排除映射"""

    @staticmethod
    def _cand(cert):
        return {"cert_no": cert, "name": f"人{cert}"}

    def test_exclude_removes_confirm_keeps_unmentioned_kept(self):
        add_list = [self._cand("X"), self._cand("A")]
        departed_list = [self._cand("Y"), self._cand("B")]
        pending_list = [self._cand("Z")]
        choices = [
            {"group": "add", "cert_no": "x", "action": "exclude"},   # 小写 x → 归一大写
            {"group": "departed", "cert_no": "Y", "action": "confirm"},
        ]
        kept_add, kept_departed, kept_pending, excluded = apply_compare_choices(
            add_list, departed_list, pending_list, choices)
        assert [c["cert_no"] for c in kept_add] == ["A"]
        assert [c["cert_no"] for c in kept_departed] == ["Y", "B"]
        assert [c["cert_no"] for c in kept_pending] == ["Z"]  # 未提及 → 默认保留
        assert excluded == {"add": ["X"], "departed": [], "pending": []}

    def test_excluded_map_uppercased(self):
        add_list = [self._cand("X")]
        _, _, _, excluded = apply_compare_choices(
            add_list, [], [], [{"group": "add", "cert_no": "x", "action": "exclude"}])
        assert excluded["add"] == ["X"]

    def test_51col_row_list_supported(self):
        # T4 契约: compare_personnel 产出的 51 列行 (证件号码在索引 3)
        add_row = [""] * 51
        add_row[1] = "张三"
        add_row[3] = "X001"
        kept_add, _, _, excluded = apply_compare_choices(
            [add_row], [], [], [{"group": "add", "cert_no": "X001", "action": "exclude"}])
        assert kept_add == []
        assert excluded["add"] == ["X001"]

    def test_empty_choices_keeps_all(self):
        add_list = [self._cand("X")]
        kept_add, _, _, excluded = apply_compare_choices(add_list, [], [], [])
        assert kept_add == add_list
        assert excluded == {"add": [], "departed": [], "pending": []}


class TestBuildCandidatePayload:
    """弹窗候选 JSON 组装"""

    @staticmethod
    def _row(cert, name):
        row = [""] * 51
        row[1] = name
        row[3] = cert
        return row

    def test_reason_single_and_multi_source(self):
        add_list = [
            self._row("A001", "张三"),
            self._row("B002", "李四"),
            self._row("C003", "王五"),
            self._row("D004", "赵六"),
        ]
        member_sets = {
            "paid": {"A001", "B002"},
            "unpaid": {"B002", "C003"},
            "contract": {"C003", "D004"},
        }
        unit_map = {"A001": "结算单元A", "B002": "结算单元B"}
        candidates, counts = build_candidate_payload(add_list, [], [], member_sets, unit_map)
        # 合同为排他标签 (与 build_verify_row 一致): C003 有未发薪则合同被抑制
        assert candidates["add"] == [
            {"cert_no": "A001", "name": "张三", "unit": "结算单元A", "reason": "发薪"},
            {"cert_no": "B002", "name": "李四", "unit": "结算单元B", "reason": "发薪+未发薪"},
            {"cert_no": "C003", "name": "王五", "unit": "", "reason": "未发薪"},
            {"cert_no": "D004", "name": "赵六", "unit": "", "reason": "合同"},
        ]
        assert counts == {"add": 4, "departed": 0, "pending": 0}

    def test_reason_contract_exclusive_of_paid(self):
        # paid+contract 重叠: 合同被发薪抑制, 与 build_verify_row 语义一致
        add_list = [self._row("E005", "孙七")]
        member_sets = {"paid": {"E005"}, "unpaid": set(), "contract": {"E005"}}
        candidates, counts = build_candidate_payload(add_list, [], [], member_sets, {})
        assert candidates["add"] == [
            {"cert_no": "E005", "name": "孙七", "unit": "", "reason": "发薪"}]
        assert counts == {"add": 1, "departed": 0, "pending": 0}

    def test_departed_pending_fixed_reason(self):
        departed_list = [self._row("D001", "赵六")]
        pending_list = [self._row("E001", "钱七")]
        candidates, counts = build_candidate_payload([], departed_list, pending_list, {}, {})
        assert candidates["departed"] == [
            {"cert_no": "D001", "name": "赵六", "unit": "", "reason": "近期离职"}]
        assert candidates["pending"] == [
            {"cert_no": "E001", "name": "钱七", "unit": "", "reason": "待确认"}]
        assert counts == {"add": 0, "departed": 1, "pending": 1}

    def test_unit_missing_tolerated(self):
        add_list = [self._row("A001", "张三")]
        candidates, counts = build_candidate_payload(add_list, [], [], {"paid": {"A001"}}, None)
        assert candidates["add"][0]["unit"] == ""
        assert counts == {"add": 1, "departed": 0, "pending": 0}

    def test_dict_rows_supported(self):
        add_list = [{"cert_no": "A001", "name": "张三"}]
        candidates, counts = build_candidate_payload(
            add_list, [], [], {"paid": {"A001"}}, {"A001": "结算单元A"})
        assert candidates["add"] == [
            {"cert_no": "A001", "name": "张三", "unit": "结算单元A", "reason": "发薪"}]
        assert counts == {"add": 1, "departed": 0, "pending": 0}