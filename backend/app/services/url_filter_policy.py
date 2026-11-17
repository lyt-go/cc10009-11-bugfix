"""URL过滤规则的确定性口径：字段校验、URL规范化、命中计算与冲突优先级。

这个模块只做纯计算，不读写 store，便于在规则保存、快照生成和作业侧复用时
保持同一套口径。所有上限和可选值都收在常量里，接口和页面上展示的口径也来自
``policy_summary()``，避免文档、校验和命中计算各说各话。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---------------------------------------------------------------- 规则字段与上限

RULE_CODE = "规则编号"
INCLUDE = "包含模式"
EXCLUDE = "排除模式"
DOMAIN_WHITELIST = "域名白名单"
PATH_BLACKLIST = "路径黑名单"
PARAM_STRIP = "参数剥离规则"
CASE_MODE = "大小写处理"
TRAILING_SLASH = "尾部斜杠策略"
CANDIDATES = "候选URL"
MATCH_SCOPE = "匹配范围"
RULE_LIMIT = "规则上限"
CONFLICT_POLICY = "冲突策略"
REMARK = "备注"

RULE_VALUE_FIELDS = [
    RULE_CODE, INCLUDE, EXCLUDE, DOMAIN_WHITELIST, PATH_BLACKLIST,
    PARAM_STRIP, CASE_MODE, TRAILING_SLASH, CANDIDATES,
    MATCH_SCOPE, RULE_LIMIT, CONFLICT_POLICY, REMARK,
]

# 规则内容指纹不含候选URL；候选URL单独参与快照幂等键，
# 这样“只改URL”和“只改规则”都能被识别为不同范围。
RULE_CONTENT_FIELDS = [field_name for field_name in RULE_VALUE_FIELDS if field_name != CANDIDATES]

MAX_RULES = 200
MAX_PATTERNS = 50
MAX_DOMAINS = 50
MAX_PATHS = 50
MAX_STRIP_PARAMS = 50
MAX_CANDIDATES = 2000
DEFAULT_RULE_LIMIT = 1000
MAX_URL_LENGTH = 2048
MAX_PATTERN_LENGTH = 512
MAX_RULE_CODE_LENGTH = 64

CASE_MODES = ("域名小写", "全部小写", "保留原样")
CASE_MODE_DEFAULT = "域名小写"
CASE_MODE_ALIASES = {
    "域名小写": "域名小写",
    "域名不区分大小写": "域名小写",
    "host-lower": "域名小写",
    "lower-host": "域名小写",
    "domain-lower": "域名小写",
    "全部小写": "全部小写",
    "lower": "全部小写",
    "lower-all": "全部小写",
    "all-lower": "全部小写",
    "保留原样": "保留原样",
    "保留": "保留原样",
    "none": "保留原样",
    "preserve": "保留原样",
}

TRAILING_SLASH_POLICIES = ("保留", "去除", "统一添加")
TRAILING_SLASH_DEFAULT = "去除"
TRAILING_SLASH_ALIASES = {
    "保留": "保留",
    "keep": "保留",
    "去除": "去除",
    "删除": "去除",
    "strip": "去除",
    "统一添加": "统一添加",
    "添加": "统一添加",
    "ensure": "统一添加",
}

MATCH_SCOPES = ("全站", "域名", "路径")
MATCH_SCOPE_DEFAULT = "全站"
MATCH_SCOPE_ALIASES = {
    "全站": "全站",
    "完整URL": "全站",
    "url": "全站",
    "full": "全站",
    "域名": "域名",
    "host": "域名",
    "domain": "域名",
    "路径": "路径",
    "path": "路径",
}

CONFLICT_POLICIES = ("拒绝优先",)
CONFLICT_POLICY_DEFAULT = "拒绝优先"
CONFLICT_POLICY_ALIASES = {
    "拒绝优先": "拒绝优先",
    "deny-first": "拒绝优先",
    "deny": "拒绝优先",
    "fail-closed": "拒绝优先",
}

# 放行/拒绝条件同时命中时检查顺序（白名单、包含模式先判放行；
# 路径黑名单、排除模式后判拒绝；最终由“拒绝优先”收敛）。
MATCH_PRIORITY = [DOMAIN_WHITELIST, INCLUDE, PATH_BLACKLIST, EXCLUDE]
# 拒绝时对外展示的首要原因顺序，保证同一个URL每次都给出同一个原因。
DENY_REASON_PRIORITY = [PATH_BLACKLIST, EXCLUDE, INCLUDE, DOMAIN_WHITELIST]

# 参数名只允许常见的 URL 参数写法；?、=、&、空白都属于失效规则。
_PARAM_NAME_RE = re.compile(r"^[A-Za-z0-9_.~\-\[\]]+$")
_DOMAIN_LABEL_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_PATH_RE = re.compile(r"^/[^\s]*$")


def policy_summary() -> dict[str, Any]:
    """对外展示的固定口径，页面和接口共用，避免两边解释不一致。"""
    return {
        "字段": list(RULE_VALUE_FIELDS),
        "上限": {
            "规则总数": MAX_RULES,
            "单规则包含模式数": MAX_PATTERNS,
            "单规则排除模式数": MAX_PATTERNS,
            "单规则域名白名单数": MAX_DOMAINS,
            "单规则路径黑名单数": MAX_PATHS,
            "单规则参数剥离项": MAX_STRIP_PARAMS,
            "单次候选URL数": MAX_CANDIDATES,
            "单规则候选URL默认上限": DEFAULT_RULE_LIMIT,
            "URL最大长度": MAX_URL_LENGTH,
            "规则编号最大长度": MAX_RULE_CODE_LENGTH,
        },
        "默认值": {
            CASE_MODE: CASE_MODE_DEFAULT,
            TRAILING_SLASH: TRAILING_SLASH_DEFAULT,
            MATCH_SCOPE: MATCH_SCOPE_DEFAULT,
            CONFLICT_POLICY: CONFLICT_POLICY_DEFAULT,
            RULE_LIMIT: DEFAULT_RULE_LIMIT,
        },
        "匹配范围": {
            "可选值": list(MATCH_SCOPES),
            "说明": "全站匹配完整规范化URL；域名只匹配主机名；路径只匹配路径与查询串（不含协议和域名）。",
        },
        "冲突条件": {
            "可选值": list(CONFLICT_POLICIES),
            "优先级": list(MATCH_PRIORITY),
            "拒绝原因优先级": list(DENY_REASON_PRIORITY),
            "说明": "任拒绝条件命中即拒绝；放行条件与拒绝条件同时命中时按拒绝优先，结果唯一且可逐项复核，不会一半放行一半拒绝。",
        },
        "规范化参数": {
            "大小写处理": list(CASE_MODES),
            "尾部斜杠策略": list(TRAILING_SLASH_POLICIES),
            "参数剥离规则": "按参数名剥离后按参数名、参数值排序；片段（#）不参与匹配。",
            "说明": "规范化在命中计算前完成，规则快照会同时保存规范化后的URL与原始URL。",
        },
        "非法值标准": {
            "空域名": "域名白名单中出现空项、协议、端口、路径或查询串",
            "非法包含模式": "正则无法编译、为空或超过最大长度",
            "非法排除模式": "正则无法编译或超过最大长度",
            "失效参数剥离规则": "参数名为空或包含 ? = & 空白等字符",
            "非法尾部斜杠策略": "只允许 保留 / 去除 / 统一添加",
            "非法大小写处理": "只允许 域名小写 / 全部小写 / 保留原样",
            "非法匹配范围": "只允许 全站 / 域名 / 路径",
            "非法冲突策略": "只允许 拒绝优先",
            "非法URL": "缺少 http/https 协议、缺少域名、端口非法、包含空白或超过长度上限",
        },
    }


def error(field_name: str, reason: str, **extra: Any) -> dict[str, Any]:
    """统一错误结构：字段、可读原因以及定位用的下标/原值。"""
    item: dict[str, Any] = {"field": field_name, "reason": reason}
    item.update(extra)
    return item


def _flatten_text(value: Any) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return text


def split_lines(value: Any) -> list[str]:
    """模式字段按换行/分号拆分，正则里的逗号保留原样。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(split_lines(item))
        return out
    text = _flatten_text(value)
    parts: list[str] = []
    for line in text.split("\n"):
        parts.extend(line.split(";"))
    return [part.strip() for part in parts if part.strip()]


def split_items(value: Any) -> list[str]:
    """域名/路径/参数按换行、分号或逗号拆分。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(split_items(item))
        return out
    text = _flatten_text(value)
    normalized = re.sub(r"[;，,]", "\n", text)
    return [part.strip() for part in normalized.split("\n") if part.strip()]


def _raw_items(value: Any, separators: str = r"[;，,]") -> list[str]:
    """保留空项，用于识别“a.com,,b.com”这类空域名/空参数，而不是静默丢弃。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                out.extend(_raw_items(item, separators))
            elif item is None:
                out.append("")
            else:
                out.append(str(item).strip())
        return out
    text = _flatten_text(value)
    normalized = re.sub(separators, "\n", text)
    return [part.strip() for part in normalized.split("\n")]


def split_candidate_urls(value: Any) -> list[str]:
    """候选URL优先按行/数组读取；单行里保留逗号，避免截断查询参数。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                out.extend(split_candidate_urls(item))
            elif item is not None:
                out.append(str(item).strip())
        return [item for item in out if item]
    text = _flatten_text(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass
    return [line.strip() for line in text.split("\n") if line.strip()]


def _compile_patterns(value: Any, field_name: str, errors: list[dict[str, Any]]) -> list[str]:
    patterns = split_lines(value)
    if len(patterns) > MAX_PATTERNS:
        errors.append(error(field_name, f"最多允许 {MAX_PATTERNS} 条模式，当前 {len(patterns)} 条", limit=MAX_PATTERNS))
    compiled: list[str] = []
    for index, pattern in enumerate(patterns, start=1):
        if len(pattern) > MAX_PATTERN_LENGTH:
            errors.append(error(field_name, f"第 {index} 条模式超过 {MAX_PATTERN_LENGTH} 个字符", index=index, value=pattern))
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(error(field_name, f"第 {index} 条模式不是合法正则：{exc}", index=index, value=pattern))
            continue
        compiled.append(pattern)
    return compiled


def _validate_domains(value: Any, errors: list[dict[str, Any]]) -> list[str]:
    raw_items = _raw_items(value)
    # 完全留空表示“不启用域名白名单”，但显式写了分隔符却留空项要报错。
    if len(raw_items) == 1 and raw_items[0] == "":
        raw_items = []
    if len(raw_items) > MAX_DOMAINS:
        errors.append(error(DOMAIN_WHITELIST, f"最多允许 {MAX_DOMAINS} 个域名，当前 {len(raw_items)} 个", limit=MAX_DOMAINS))
    result: list[str] = []
    for index, raw in enumerate(raw_items, start=1):
        item = raw.strip().lower()
        if not item:
            errors.append(error(DOMAIN_WHITELIST, f"第 {index} 项为空域名", index=index, value=raw))
            continue
        if "://" in item:
            errors.append(error(DOMAIN_WHITELIST, f"第 {index} 项不应包含协议：{raw}", index=index, value=raw))
            continue
        if any(ch in item for ch in "/?#"):
            errors.append(error(DOMAIN_WHITELIST, f"第 {index} 项不应包含路径或查询串：{raw}", index=index, value=raw))
            continue
        host = item[2:] if item.startswith("*.") else item
        if not host:
            errors.append(error(DOMAIN_WHITELIST, f"第 {index} 项是空域名：{raw}", index=index, value=raw))
            continue
        if ":" in host:
            errors.append(error(DOMAIN_WHITELIST, f"第 {index} 项不应包含端口：{raw}", index=index, value=raw))
            continue
        if not (_DOMAIN_LABEL_RE.match(host) or _IPV4_RE.match(host) or host == "localhost"):
            errors.append(error(DOMAIN_WHITELIST, f"第 {index} 项域名格式非法：{raw}", index=index, value=raw))
            continue
        if item not in result:
            result.append(item)
    return result


def _validate_paths(value: Any, errors: list[dict[str, Any]]) -> list[str]:
    patterns = split_lines(value)
    if len(patterns) > MAX_PATHS:
        errors.append(error(PATH_BLACKLIST, f"最多允许 {MAX_PATHS} 条路径，当前 {len(patterns)} 条", limit=MAX_PATHS))
    result: list[str] = []
    for index, raw in enumerate(patterns, start=1):
        item = raw.strip()
        if not item:
            errors.append(error(PATH_BLACKLIST, f"第 {index} 项为空路径", index=index, value=raw))
            continue
        if item.startswith("re:"):
            pattern = item[3:]
            if not pattern:
                errors.append(error(PATH_BLACKLIST, f"第 {index} 项 re: 后缺少正则", index=index, value=raw))
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(error(PATH_BLACKLIST, f"第 {index} 条路径正则非法：{exc}", index=index, value=raw))
                continue
        elif not _PATH_RE.match(item):
            errors.append(error(PATH_BLACKLIST, f"第 {index} 项路径必须以 / 开头且不含空白：{raw}", index=index, value=raw))
            continue
        if item not in result:
            result.append(item)
    return result


def _validate_params(value: Any, errors: list[dict[str, Any]]) -> list[str]:
    raw_items = _raw_items(value)
    if len(raw_items) == 1 and raw_items[0] == "":
        raw_items = []
    if len(raw_items) > MAX_STRIP_PARAMS:
        errors.append(error(PARAM_STRIP, f"最多允许 {MAX_STRIP_PARAMS} 个参数，当前 {len(raw_items)} 个", limit=MAX_STRIP_PARAMS))
    result: list[str] = []
    for index, raw in enumerate(raw_items, start=1):
        item = raw.strip()
        if not item:
            errors.append(error(PARAM_STRIP, f"第 {index} 项为空参数名", index=index, value=raw))
            continue
        if not _PARAM_NAME_RE.match(item):
            errors.append(error(PARAM_STRIP, f"第 {index} 项参数名失效：{raw}，只允许字母、数字、_ . ~ - [ ]", index=index, value=raw))
            continue
        if item not in result:
            result.append(item)
    return result


def _parse_int(
    value: Any,
    field_name: str,
    default: int,
    low: int,
    high: int,
    errors: list[dict[str, Any]],
) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        errors.append(error(field_name, f"{field_name}必须是整数，当前值：{value}", value=value))
        return default
    if number < low or number > high:
        errors.append(error(field_name, f"{field_name}必须在 {low} 到 {high} 之间，当前值：{number}", value=value))
        return default
    return number


def _normalize_enum(
    value: Any,
    field_name: str,
    aliases: dict[str, str],
    default: str,
    allowed: Iterable[str],
    errors: list[dict[str, Any]],
) -> str:
    if value is None or str(value).strip() == "":
        return default
    text = str(value).strip()
    if text in aliases:
        return aliases[text]
    if text.lower() in aliases:
        return aliases[text.lower()]
    errors.append(error(field_name, f"{field_name}非法：{text}，只允许 {' / '.join(allowed)}", value=text))
    return default


def _validate_candidates(value: Any, limit: int, errors: list[dict[str, Any]]) -> list[str]:
    candidates = split_candidate_urls(value)
    if len(candidates) > MAX_CANDIDATES:
        errors.append(error(CANDIDATES, f"单次最多处理 {MAX_CANDIDATES} 个候选URL，当前 {len(candidates)} 个", limit=MAX_CANDIDATES, count=len(candidates)))
        return candidates
    if len(candidates) > limit:
        errors.append(error(CANDIDATES, f"候选URL数 {len(candidates)} 超过规则上限 {limit}", limit=limit, count=len(candidates)))
    for index, raw in enumerate(candidates, start=1):
        problem = _url_shape_error(raw)
        if problem:
            errors.append(error(CANDIDATES, f"候选URL第 {index} 项{problem}", index=index, value=raw))
    return candidates


def _url_shape_error(raw: str) -> str | None:
    if not raw:
        return "为空"
    if len(raw) > MAX_URL_LENGTH:
        return f"超过 {MAX_URL_LENGTH} 个字符"
    if any(ch.isspace() for ch in raw):
        return "包含空白字符"
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        return f"无法解析：{exc}"
    if parts.scheme.lower() not in ("http", "https"):
        return "协议必须是 http/https"
    if not parts.hostname:
        return "缺少域名"
    try:
        port = parts.port
    except ValueError:
        return "端口非法"
    if port is not None and not (1 <= port <= 65535):
        return "端口非法"
    return None


def url_shape_error(raw: str) -> str | None:
    """URL 形态检查：协议、域名、端口、空白、长度；返回可读原因或 None。"""
    return _url_shape_error(raw)


def validate_rule_values(
    values: dict[str, Any],
    *,
    existing_rows: list[dict[str, Any]] | None = None,
    entry_id: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """合并并校验一条完整规则，返回规范化后的字段和可定位的错误列表。

    校验在写入前一次做完，任何一项失败都不会产生“部分字段已保存”的中间状态。
    """
    existing_rows = existing_rows or []
    errors: list[dict[str, Any]] = []

    code = str(values.get(RULE_CODE) or "").strip()
    if not code:
        errors.append(error(RULE_CODE, "规则编号不能为空"))
    elif len(code) > MAX_RULE_CODE_LENGTH:
        errors.append(error(RULE_CODE, f"规则编号不能超过 {MAX_RULE_CODE_LENGTH} 个字符", value=code))
    else:
        for row in existing_rows:
            if entry_id is not None and int(row.get("id", 0)) == int(entry_id):
                continue
            if str(row.get(RULE_CODE) or "").strip().lower() == code.lower():
                errors.append(error(RULE_CODE, f"规则编号 {code} 已存在，不能重复登记", value=code))
                break
    active_count = len([row for row in existing_rows if row.get("status") != "已废弃"])
    if entry_id is None and active_count >= MAX_RULES:
        errors.append(error(RULE_CODE, f"过滤规则总数已达到上限 {MAX_RULES}，请先废弃不再使用的规则", limit=MAX_RULES))

    include = _compile_patterns(values.get(INCLUDE), INCLUDE, errors)
    if not split_lines(values.get(INCLUDE)):
        errors.append(error(INCLUDE, "包含模式不能为空"))
    exclude = _compile_patterns(values.get(EXCLUDE), EXCLUDE, errors)
    if not split_lines(values.get(EXCLUDE)):
        # 与原接口的必填口径保持一致：不想排除任何URL时显式写一个永不匹配的正则。
        errors.append(error(EXCLUDE, "排除模式不能为空"))
    domains = _validate_domains(values.get(DOMAIN_WHITELIST), errors)
    paths = _validate_paths(values.get(PATH_BLACKLIST), errors)
    params = _validate_params(values.get(PARAM_STRIP), errors)

    case_mode = _normalize_enum(values.get(CASE_MODE), CASE_MODE, CASE_MODE_ALIASES, CASE_MODE_DEFAULT, CASE_MODES, errors)
    trailing = _normalize_enum(values.get(TRAILING_SLASH), TRAILING_SLASH, TRAILING_SLASH_ALIASES, TRAILING_SLASH_DEFAULT, TRAILING_SLASH_POLICIES, errors)
    scope = _normalize_enum(values.get(MATCH_SCOPE), MATCH_SCOPE, MATCH_SCOPE_ALIASES, MATCH_SCOPE_DEFAULT, MATCH_SCOPES, errors)
    conflict = _normalize_enum(values.get(CONFLICT_POLICY), CONFLICT_POLICY, CONFLICT_POLICY_ALIASES, CONFLICT_POLICY_DEFAULT, CONFLICT_POLICIES, errors)
    rule_limit = _parse_int(values.get(RULE_LIMIT), RULE_LIMIT, DEFAULT_RULE_LIMIT, 1, MAX_CANDIDATES, errors)
    candidates = _validate_candidates(values.get(CANDIDATES), rule_limit, errors)
    remark = str(values.get(REMARK) or "").strip()

    normalized = {
        RULE_CODE: code,
        INCLUDE: "\n".join(include),
        EXCLUDE: "\n".join(exclude),
        DOMAIN_WHITELIST: ",".join(domains),
        PATH_BLACKLIST: "\n".join(paths),
        PARAM_STRIP: ",".join(params),
        CASE_MODE: case_mode,
        TRAILING_SLASH: trailing,
        CANDIDATES: candidates,
        MATCH_SCOPE: scope,
        RULE_LIMIT: rule_limit,
        CONFLICT_POLICY: conflict,
        REMARK: remark,
    }
    return normalized, errors


# ---------------------------------------------------------------- 规则快照与指纹


def rule_fingerprint(rule: dict[str, Any], candidates: list[str] | None = None) -> str:
    """规则内容和候选URL的稳定指纹，用于幂等键与“是否换版”的判断。"""
    payload = {field_name: rule.get(field_name) for field_name in RULE_CONTENT_FIELDS}
    payload[CANDIDATES] = list(candidates if candidates is not None else split_candidate_urls(rule.get(CANDIDATES)))
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def material_rule_fields(rule: dict[str, Any]) -> dict[str, Any]:
    return {field_name: rule.get(field_name) for field_name in RULE_CONTENT_FIELDS}


def candidate_list(rule: dict[str, Any]) -> list[str]:
    candidates = rule.get(CANDIDATES)
    if isinstance(candidates, list):
        return [str(item).strip() for item in candidates if str(item).strip()]
    return split_candidate_urls(candidates)


def snapshot_idempotency_key(rule_id: int, version: int, fingerprint: str, candidates: list[str]) -> str:
    """同一规则、同一版本、同一内容与同一候选URL只允许对应一份快照。"""
    encoded = json.dumps(
        {"rule_id": int(rule_id), "version": int(version), "fingerprint": fingerprint, "candidates": list(candidates)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def snapshot_checksum(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- URL 规范化与判断


@dataclass
class MatchHit:
    field: str
    value: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"命中字段": self.field, "命中值": self.value, "命中说明": self.detail}


@dataclass
class UrlDecision:
    original: str
    normalized: str
    admitted: bool
    primary_hit: MatchHit
    hits: list[MatchHit] = field(default_factory=list)
    conflict: bool = False
    reason: str = ""
    duplicate_count: int = 1
    normalization_error: str | None = None
    normalization: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "原始URL": self.original,
            "规范化URL": self.normalized,
            "决定": "准入" if self.admitted else "拒绝",
            "命中": self.primary_hit.field,
            "命中值": self.primary_hit.value,
            "命中说明": self.primary_hit.detail,
            "命中项": [hit.as_dict() for hit in self.hits],
            "冲突": self.conflict,
            "原因": self.reason,
            "重复次数": self.duplicate_count,
            "规范化说明": self.normalization,
        }


def _normalize_query(query: str) -> tuple[list[tuple[str, str]], dict[str, Any] | None]:
    try:
        return parse_qsl(query, keep_blank_values=True, strict_parsing=False), None
    except ValueError as exc:
        return [], error("URL", f"查询串无法解析：{exc}")


def normalize_url_detail(
    raw: str,
    rule: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any]]:
    """规范化URL，同时返回中间信息，便于解释“尾部斜杠/参数剥离”为什么这样处理。"""
    raw = str(raw).strip()
    detail: dict[str, Any] = {
        "strip_params": [],
        "case_mode": rule.get(CASE_MODE) or CASE_MODE_DEFAULT,
        "trailing_slash": rule.get(TRAILING_SLASH) or TRAILING_SLASH_DEFAULT,
        "original_path": "",
        "final_path": "",
        "original_query": "",
        "final_query": "",
        "removed_params": [],
        "query_sorted": False,
    }
    shape_error = _url_shape_error(raw)
    if shape_error:
        return None, error("URL", f"URL{shape_error}", value=raw), detail

    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        return None, error("URL", f"URL无法解析：{exc}", value=raw), detail

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    if parts.username is not None:
        credentials = parts.username
        if parts.password is not None:
            credentials += f":{parts.password}"
        netloc = f"{credentials}@{netloc}"

    case_mode = rule.get(CASE_MODE) or CASE_MODE_DEFAULT
    trailing = rule.get(TRAILING_SLASH) or TRAILING_SLASH_DEFAULT
    strip_list = sorted({param.lower() for param in split_items(rule.get(PARAM_STRIP))})
    strip_set = set(strip_list)
    detail["strip_params"] = strip_list

    path = parts.path or "/"
    detail["original_path"] = path
    detail["original_query"] = parts.query
    query_pairs, query_error = _normalize_query(parts.query)
    if query_error is not None:
        return None, query_error, detail
    if strip_set:
        kept: list[tuple[str, str]] = []
        for key, value in query_pairs:
            if key.lower() in strip_set:
                detail["removed_params"].append(key)
            else:
                kept.append((key, value))
        query_pairs = kept
    original_order = list(query_pairs)
    query_pairs.sort(key=lambda pair: (pair[0], pair[1]))
    detail["query_sorted"] = query_pairs != original_order
    query = urlencode(query_pairs, doseq=True)

    if case_mode == "全部小写":
        path = path.lower()
        query = query.lower()

    if trailing == "去除":
        if path != "/":
            path = path.rstrip("/") or "/"
    elif trailing == "统一添加":
        path = path if path.endswith("/") else f"{path}/"
    else:  # 保留
        path = path or "/"
    if not path:
        path = "/"
    detail["final_path"] = path
    detail["final_query"] = query

    normalized = urlunsplit((scheme, netloc, path, query, ""))
    return normalized, None, detail


def normalize_url(raw: str, rule: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """按规则声明的规范化参数返回稳定URL；无法规范化时给出可定位原因。"""
    normalized, problem, _ = normalize_url_detail(raw, rule)
    return normalized, problem


def _domain_matches(host: str, entry: str) -> bool:
    host = host.lower().rstrip(".")
    entry = entry.lower().rstrip(".")
    if entry.startswith("*."):
        suffix = entry[2:]
        return host.endswith("." + suffix) and host != suffix
    return host == entry or host.endswith("." + entry)


def _path_matches(path: str, entry: str) -> bool:
    if entry.startswith("re:"):
        try:
            return re.search(entry[3:], path) is not None
        except re.error:
            return False
    if "*" in entry or "?" in entry:
        return fnmatch.fnmatchcase(path, entry)
    clean = entry.rstrip("/") or "/"
    return path == clean or path.startswith(clean.rstrip("/") + "/")


def _search_patterns(patterns: list[str], target: str) -> str | None:
    for pattern in patterns:
        try:
            if re.search(pattern, target) is not None:
                return pattern
        except re.error:
            continue
    return None


def evaluate_url(raw: str, rule: dict[str, Any]) -> UrlDecision:
    """计算一个URL在规则下的唯一结果，冲突按“拒绝优先”收敛。"""
    normalized, normalization_error, normalization_detail = normalize_url_detail(raw, rule)
    if normalization_error is not None:
        hit = MatchHit("URL规范化", "", normalization_error["reason"])
        return UrlDecision(
            original=raw,
            normalized=str(raw).strip(),
            admitted=False,
            primary_hit=hit,
            hits=[hit],
            conflict=False,
            reason=normalization_error["reason"],
            duplicate_count=1,
            normalization_error=normalization_error["reason"],
            normalization=normalization_detail,
        )
    assert normalized is not None

    parts = urlsplit(normalized)
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    scope = rule.get(MATCH_SCOPE) or MATCH_SCOPE_DEFAULT
    target = normalized
    if scope == "域名":
        target = host
    elif scope == "路径":
        target = path + (f"?{parts.query}" if parts.query else "")

    allow_hits: list[MatchHit] = []
    deny_hits: list[MatchHit] = []

    domains = split_items(rule.get(DOMAIN_WHITELIST))
    if domains:
        matched_domain = next((domain for domain in domains if _domain_matches(host, domain)), None)
        if matched_domain:
            allow_hits.append(MatchHit(DOMAIN_WHITELIST, matched_domain, f"域名 {host} 命中白名单"))
        else:
            deny_hits.append(MatchHit(DOMAIN_WHITELIST, ",".join(domains), f"域名 {host} 不在白名单内"))

    include_patterns = split_lines(rule.get(INCLUDE))
    include_match = _search_patterns(include_patterns, target)
    if include_match is not None:
        allow_hits.append(MatchHit(INCLUDE, include_match, f"匹配包含模式 {include_match}"))
    else:
        deny_hits.append(MatchHit(INCLUDE, "\n".join(include_patterns), f"未命中任何包含模式（范围：{scope}）"))

    path_entries = split_lines(rule.get(PATH_BLACKLIST))
    path_match = next((entry for entry in path_entries if _path_matches(path, entry)), None)
    if path_match is not None:
        deny_hits.append(MatchHit(PATH_BLACKLIST, path_match, f"路径 {path} 命中黑名单 {path_match}"))

    exclude_patterns = split_lines(rule.get(EXCLUDE))
    exclude_match = _search_patterns(exclude_patterns, target)
    if exclude_match is not None:
        deny_hits.append(MatchHit(EXCLUDE, exclude_match, f"匹配排除模式 {exclude_match}"))

    conflict = bool(allow_hits and deny_hits)
    admitted = not deny_hits
    if admitted:
        primary = allow_hits[0] if allow_hits else MatchHit(INCLUDE, "", "准入")
        reason = primary.detail
    else:
        order = {name: index for index, name in enumerate(DENY_REASON_PRIORITY)}
        deny_hits.sort(key=lambda hit: order.get(hit.field, 99))
        primary = deny_hits[0]
        reason = primary.detail
        if conflict:
            reason = f"{reason}；同时命中放行条件，按冲突策略拒绝优先"
    return UrlDecision(
        original=raw,
        normalized=normalized,
        admitted=admitted,
        primary_hit=primary,
        hits=allow_hits + deny_hits,
        conflict=conflict,
        reason=reason,
        duplicate_count=1,
        normalization=normalization_detail,
    )


def build_decisions(
    candidates: Iterable[str],
    rule: dict[str, Any],
) -> tuple[list[UrlDecision], list[dict[str, Any]]]:
    """对候选URL做规范化、去重和唯一命中计算；返回去重后的结果和错误。"""
    decisions: list[UrlDecision] = []
    errors: list[dict[str, Any]] = []
    by_normalized: dict[str, UrlDecision] = {}
    for index, raw in enumerate(candidates, start=1):
        decision = evaluate_url(raw, rule)
        if decision.normalization_error is not None:
            errors.append(error(CANDIDATES, f"候选URL第 {index} 项无法规范化：{decision.reason}", index=index, value=raw))
            continue
        existing = by_normalized.get(decision.normalized)
        if existing is not None:
            existing.duplicate_count += 1
            continue
        by_normalized[decision.normalized] = decision
        decisions.append(decision)
    return decisions, errors


def summarize_decisions(decisions: list[UrlDecision]) -> dict[str, Any]:
    hit_counter: dict[str, int] = {}
    for decision in decisions:
        hit_counter[decision.primary_hit.field] = hit_counter.get(decision.primary_hit.field, 0) + 1
    return {
        "候选URL数": sum(decision.duplicate_count for decision in decisions),
        "去重后URL数": len(decisions),
        "准入数": sum(1 for decision in decisions if decision.admitted),
        "拒绝数": sum(1 for decision in decisions if not decision.admitted),
        "冲突数": sum(1 for decision in decisions if decision.conflict),
        "命中统计": hit_counter,
    }
