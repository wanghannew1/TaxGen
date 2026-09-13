"""增减员比对二次确认 - roster 映射与 choices 纯函数模块

将持久化个税端名单 (config_db.get_tax_roster) 映射为与
tax_export_parser.parse_tax_export() 完全一致的 17 键导出格式,
并提供两阶段确认流程所需的 choices 校验/应用与候选 payload 组装。

本模块为纯函数, 不 import Flask / oracledb, 不触任何数据库。
"""
import json
from typing import Any, Dict, List, Tuple

from templates_gen.personnel_info import get_birth_date_from_id, get_gender_from_id

# 与 tax_export_parser.parse_tax_export() 返回的 17 键完全一致
EXPORT_KEYS: List[str] = [
    "工号", "姓名", "证件类型", "证件号码", "性别", "出生日期", "报送状态",
    "身份验证状态", "手机号码", "任职受雇从业日期", "离职日期", "是否扣除减除费用",
    "任职受雇从业类型", "其他情况说明", "国籍", "备注", "更新时间",
]

# 51 列行中常用字段的列索引 (与 personnel_compare.COMPARE_HEADERS 一致)
_IDX_姓名 = 1
_IDX_证件号码 = 3

# choices 合法取值
_VALID_GROUPS = {"add", "departed", "pending"}
_VALID_ACTIONS = {"confirm", "exclude"}


def _cert_key(value) -> str:
    """证件号归一: 去空格转大写 (与 personnel_compare._cert_key 一致)。"""
    return str(value or "").strip().upper()


def _item_cert_no(item) -> str:
    """从候选元素提取证件号 (大写归一)。

    兼容两种候选形态:
    - dict (含 cert_no 键, 或 51 列导出格式的 证件号码 键)
    - 51 列行 (list, 证件号码在索引 3, compare_personnel 产出)
    """
    if isinstance(item, dict):
        return _cert_key(item.get("cert_no") or item.get("证件号码"))
    try:
        return _cert_key(item[_IDX_证件号码])
    except (IndexError, TypeError):
        return ""


def _item_name(item) -> str:
    """从候选元素提取姓名。"""
    if isinstance(item, dict):
        return str(item.get("name") or item.get("姓名") or "")
    try:
        return str(item[_IDX_姓名] or "")
    except (IndexError, TypeError):
        return ""


def map_tax_roster_to_export(row: Dict) -> Dict[str, str]:
    """把 config_db.get_tax_roster() 的行映射为 17 键导出格式 dict。

    与 tax_export_parser.parse_tax_export() 返回的键完全一致 (展平 dict,
    全部值为 str)。映射规则:
    - 姓名→name, 证件类型→cert_type, 证件号码→cert_no, 工号→emp_no
    - 任职受雇从业日期→hire_date, 离职日期→leave_date
    - 性别/出生日期: 证件类型为身份证时由证件号推导, 否则留空
    - 任职受雇从业类型→"雇员"; 国籍为空→"中国"
    - 其余键 (报送状态/身份验证状态/手机号码/是否扣除减除费用/
      其他情况说明/备注/更新时间) 一律空串
    """
    cert_no = str(row.get("cert_no") or "")
    cert_type = str(row.get("cert_type") or "")
    if "身份证" in cert_type:
        gender = get_gender_from_id(cert_no)
        birth_date = get_birth_date_from_id(cert_no)
    else:
        gender = ""
        birth_date = ""
    return {
        "工号": str(row.get("emp_no") or ""),
        "姓名": str(row.get("name") or ""),
        "证件类型": cert_type,
        "证件号码": cert_no,
        "性别": gender,
        "出生日期": birth_date,
        "报送状态": "",
        "身份验证状态": "",
        "手机号码": "",
        "任职受雇从业日期": str(row.get("hire_date") or ""),
        "离职日期": str(row.get("leave_date") or ""),
        "是否扣除减除费用": "",
        "任职受雇从业类型": "雇员",
        "其他情况说明": "",
        "国籍": str(row.get("nationality") or "") or "中国",
        "备注": "",
        "更新时间": "",
    }


def validate_choices(raw_choices: Any) -> List[Dict]:
    """解析并校验前端提交的 choices JSON 字符串数组。

    任何一项非法 (group/action/cert_no) 均抛 ValueError (含具体项错误信息),
    非法项在列表尾部同样被拒绝而非截断。合法返回规范化列表:
    [{"group", "cert_no", "action"}], cert_no 统一去空格转大写。
    """
    if not isinstance(raw_choices, str):
        raise ValueError("choices 必须是 JSON 字符串数组")
    try:
        parsed = json.loads(raw_choices)
    except json.JSONDecodeError as e:
        raise ValueError(f"choices 不是合法 JSON: {e}") from e
    if not isinstance(parsed, list):
        raise ValueError("choices 必须是 JSON 数组")
    result = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ValueError(f"choices[{i}] 必须是对象")
        group = item.get("group")
        action = item.get("action")
        cert_no = item.get("cert_no")
        if group not in _VALID_GROUPS:
            raise ValueError(f"choices[{i}] group 非法: {group!r}")
        if action not in _VALID_ACTIONS:
            raise ValueError(f"choices[{i}] action 非法: {action!r}")
        if not isinstance(cert_no, str) or not cert_no.strip():
            raise ValueError(f"choices[{i}] cert_no 缺失或为空")
        result.append({"group": group, "cert_no": _cert_key(cert_no), "action": action})
    return result


def apply_compare_choices(add_list: List, departed_list: List, pending_list: List,
                          choices: List[Dict]) -> Tuple[List, List, List, Dict]:
    """按 choices 应用排除, 返回保留列表 + 分组排除映射。

    action=exclude 的证件号从对应分组候选列表移除; choices 中未提及的
    候选默认按「确认」保留。候选元素只按 cert_no 匹配 (兼容 dict 与
    51 列行两种形态)。excluded_map 为 {"add": [...], "departed": [...],
    "pending": [...]}, 证件号大写。
    """
    excluded = {"add": [], "departed": [], "pending": []}
    for ch in choices:
        if ch["action"] == "exclude":
            excluded[ch["group"]].append(_cert_key(ch["cert_no"]))
    excluded_sets = {g: set(certs) for g, certs in excluded.items()}
    kept_add = [item for item in add_list
                if _item_cert_no(item) not in excluded_sets["add"]]
    kept_departed = [item for item in departed_list
                     if _item_cert_no(item) not in excluded_sets["departed"]]
    kept_pending = [item for item in pending_list
                    if _item_cert_no(item) not in excluded_sets["pending"]]
    return kept_add, kept_departed, kept_pending, excluded


def _derive_add_reason(cert: str, member_sets: Dict) -> str:
    """增员候选 reason 推导: 按成员身份集合 (发薪/未发薪/合同) 多源 "+" 连接。

    与 build_verify_row 的 reason_str 逻辑一致 (personnel_compare.py:289-296):
    合同为排他标签, 仅当无发薪且无未发薪时标记 (has_contract = contract_start
    and not has_paid and not has_unpaid)。
    """
    paid = set(member_sets.get("paid") or set())
    unpaid = set(member_sets.get("unpaid") or set())
    contract = set(member_sets.get("contract") or set())
    reasons = []
    if cert in paid:
        reasons.append("发薪")
    if cert in unpaid:
        reasons.append("未发薪")
    if cert in contract and cert not in paid and cert not in unpaid:
        reasons.append("合同")
    return "+".join(reasons)


def build_candidate_payload(add_list: List, departed_list: List, pending_list: List,
                            member_sets: Dict, unit_map: Dict) -> Tuple[Dict, Dict]:
    """组装弹窗候选 JSON。

    每个候选 {"cert_no", "name", "unit", "reason"}:
    - add 候选 reason 由 member_sets ({"paid", "unpaid", "contract"} 三元身份
      集合) 推导, departed 固定 "近期离职", pending 固定 "待确认"
    - unit 从 unit_map (cert_no→结算单元名) 映射, 缺失容忍为空串
    返回 (candidates, counts): candidates 键为 {"add", "departed", "pending"},
    counts 为 {"add": n, "departed": n, "pending": n}。
    """
    unit_map = unit_map or {}
    member_sets = member_sets or {}

    def _candidate(item, reason):
        cert = _item_cert_no(item)
        return {"cert_no": cert, "name": _item_name(item),
                "unit": str(unit_map.get(cert) or ""), "reason": reason}

    add_candidates = [_candidate(item, _derive_add_reason(_item_cert_no(item), member_sets))
                      for item in add_list]
    departed_candidates = [_candidate(item, "近期离职") for item in departed_list]
    pending_candidates = [_candidate(item, "待确认") for item in pending_list]

    candidates = {"add": add_candidates, "departed": departed_candidates,
                  "pending": pending_candidates}
    counts = {"add": len(add_candidates), "departed": len(departed_candidates),
              "pending": len(pending_candidates)}
    return candidates, counts