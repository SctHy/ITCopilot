"""Context/checksum rules adapted from Magpie 1248dc16 internal/redact.

MIT Copyright (c) 2026 yetone; attribution/license: docs/licenses/Magpie-MIT.txt.
No external calls or dictionaries; these rules share the storage/outbound policy.
"""
from __future__ import annotations

from datetime import date
import re

IBAN_LENGTHS = dict(zip(
    "AD AE AL AT AZ BA BE BG BH BI BR BY CH CR CY CZ DE DJ DK DO EE EG ES FI FK FO FR GB GE GI GL GR GT HN HR HU IE IL IQ IS IT JO KW KZ LB LC LI LT LU LV LY MC MD ME MK MN MR MT MU NI NL NO OM PK PL PS PT QA RO RS RU SA SC SD SE SI SK SM SO ST SV TL TN TR UA VA VG XK YE".split(),
    [24,23,28,20,28,20,16,22,22,27,29,28,21,22,28,24,22,27,18,28,20,29,24,18,18,18,27,22,22,23,18,27,28,28,21,28,22,23,23,26,27,30,30,20,28,32,21,20,20,21,25,27,24,22,19,20,27,31,30,28,18,15,23,24,28,29,25,29,24,22,33,24,31,18,24,19,24,27,23,25,28,23,24,26,29,22,24,20,30], strict=True))
_IBAN = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30})")
_SSN = re.compile(r"(?<![A-Za-z0-9_-])\d{3}-\d{2}-\d{4}(?![A-Za-z0-9_-])")
_NAMED_SSN = re.compile(r"(?i)(?:\bssn\b|\bsocial[ _-]+security(?:[ _-]+(?:number|no\.?))?|社会安全号码)[\s\"']*(?:[:：=]|is|为|是)?[ \t\"']*(\d{9}|\d{3} \d{2} \d{4})(?!\d)")
_PASSPORT = re.compile(r"(?i)(?:\bpassport(?:[ _-]*(?:no\.?|number|num))?|\breisepass(?:nummer)?|护照(?:号码|号)?|護照(?:號碼|號)?)[ \t\"']*(?:[:：=#]|is|was|是|为)?[ \t\"']*([A-Z0-9]{6,9})(?![A-Za-z0-9])")
_DRIVER = re.compile(r"(?i)(?:\bdriver(?:'s|s)?[ _-]+licen[cs]e(?:[ _-]+(?:number|no\.?))?|驾驶证(?:号码|号)?)[ \t\"']*(?:[:：=#]|is|是|为)?[ \t\"']*([A-Z0-9-]{6,20})(?![A-Za-z0-9])")
_PHONE = re.compile(r"(?i)(?:\b(?:phone|telephone|mobile|tel)(?:[ _-]*(?:number|no\.?))?|电话号码|联系电话|手机号|电话)[ \t\"']*(?:[:：=]|is|为|是)?[ \t\"']*(\+?\d[\d ().-]{5,28}\d(?:[ \t]*(?:x|ext\.?)[ \t]*\d{1,6})?)(?![A-Za-z0-9_])")

_BUSINESS_FIELD = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9_])(?:employee|customer|device|asset|order|transaction)[ _-]*(?:id(?:entifier)?|number|no\.?)"
    r"|订单(?:号|编号)|工号|员工编号|客户编号|设备(?:号|编号|卡号)|资产(?:号|编号))"
    r"[ \t*\"']*(?:[:：=,]|\bis\b|\bof\b|为|是)?[ \t*\"']*$")
_CN_MOBILE = re.compile(r"(?<![A-Za-z0-9_])(?:\+?86[ -]?|0086[ -]?)?1[3-9][0-9][ -]?[0-9]{4}[ -]?[0-9]{4}(?![A-Za-z0-9_])")
_SPACED_CN_ID = re.compile(r"(?<![A-Za-z0-9_])[1-9][0-9]{5}[ -][0-9]{8}[ -]?[0-9]{3}[0-9Xx](?![A-Za-z0-9_])")
_CN_PLATE = re.compile(r"(?<![A-Za-z0-9_])[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{5,6}(?![A-Za-z0-9_]|\.[A-Za-z])", re.I)
_CN_PASSPORT = re.compile(r"(?<![A-Za-z0-9_])[EG][0-9]{8}(?![A-Za-z0-9_]|\.[A-Za-z])", re.I)
_NAMED_CARD = re.compile(
    r"(?i)(?:银行卡(?:号)?|銀行卡(?:號)?|信用卡(?:号|號)?|卡号|卡號|\b(?:credit|debit)[ _-]+card(?:[ _-]+(?:number|no\.?))?)"
    r"[ \t*\"']*(?:[:：=]|\bis\b|是|为)?[ \t*\"']*([2-6](?:[ -]?[0-9]){12,18})(?![A-Za-z0-9_]|[ -][0-9])")
_CONTEXT_CARD = re.compile(r"(?<![A-Za-z0-9_])(?<![0-9][ -])[2-6](?:[ -]?[0-9]){12,18}(?![A-Za-z0-9_]|[ -][0-9])")


def business_identifier_context(text, start):
    return bool(_BUSINESS_FIELD.search(text[max(0, start - 96):start]))


def chinese_identifier_spans(text):
    for match in _CN_MOBILE.finditer(text):
        if not business_identifier_context(text, match.start()):
            yield *match.span(), "PHONE"
    for match in _SPACED_CN_ID.finditer(text):
        if chinese_id_valid(re.sub(r"[ -]", "", match[0])) and not business_identifier_context(text, match.start()):
            yield *match.span(), "ID"
    for match in _CN_PLATE.finditer(text):
        before = text[max(0, match.start()-24):match.start()]
        # The province/letter shape alone is insufficient for all-letter product
        # codes. A plate cue can also protect a mistyped/nonstandard plate.
        if (any(c.isdigit() for c in match[0]) or re.search(r"车牌|車牌|号牌|牌照", before)) and not business_identifier_context(text, match.start()):
            yield *match.span(), "ID"
    for match in _CN_PASSPORT.finditer(text):
        before = re.split(r"[\n，。；;!?！？]", text[max(0, match.start()-32):match.start()])[-1]
        if (re.search(r"护照|護照|证件号|證件號", before)
                and not re.search(r"版本|字段|格式|正则|模块|插件", before)
                and not business_identifier_context(text, match.start())):
            yield *match.span(), "ID"
    for match in _NAMED_CARD.finditer(text):
        value = re.sub(r"[ -]", "", match[1])
        if len(set(value)) > 1 and not business_identifier_context(text, match.start(1)):
            # Explicit financial meaning protects mistyped check digits too;
            # unlabeled PANs still require the original Luhn check.
            yield *match.span(1), "ACCOUNT"
    for match in _CONTEXT_CARD.finditer(text):
        before = re.split(r"[\n，。；;!?！？]", text[max(0, match.start()-32):match.start()])[-1]
        after = text[match.end():match.end()+24]
        transfer = re.search(r"(?:转账|轉帳|打钱|汇款|匯款|付款|收款)[^\d\n，。；;!?！？]{0,20}卡[ \t:：]*$", before)
        trailing_label = re.match(r"[ \t]*(?:[\u3400-\u9fff]{2,4}的)?(?:银行卡|銀行卡|信用卡)(?:号|號)?", after)
        if trailing_label and (re.match(r"[ \t:：=]*[0-9]", after[trailing_label.end():])
                               or re.search(r"(?:身份证(?:号码|号)?|证件(?:号码|号)?)[ \t:：=]*$", before)):
            # In '身份证 <id> 银行卡 <pan>', 银行卡 labels the NEXT number.
            trailing_label = None
        if ((transfer or trailing_label) and len(set(re.sub(r"[ -]", "", match[0]))) > 1
                and not business_identifier_context(text, match.start())):
            yield *match.span(), "ACCOUNT"

# Like Magpie's named passport rule: the field's meaning, a complete value,
# and a constrained shape must agree. Bare identifiers are never sufficient.
_FIELD_JOIN = r"[ \t*\"']*(?:[:：=#]|\bis\b|\bwas\b|是|为)?[ \t*\"'(]*"
_NUMBER_LABEL = r"(?:number|no\.?|num|id)"
_NAMED_IDENTIFIERS = (
    ("ACCOUNT", re.compile(
        r"(?i)(?<![\w.-])(?:bank[ _-]+account(?:[ _-]+" + _NUMBER_LABEL + r")?"
        r"|account[ _-]+(?:number|no\.?|num)|acct(?:[ _-]+" + _NUMBER_LABEL + r")?"
        r"|routing[ _-]+(?:number|no\.?)|bank[ _-]+routing[ _-]+(?:number|no\.?)|bban)(?![\w-])"
        + _FIELD_JOIN + r"([A-Z0-9][A-Z0-9-]{4,32}[A-Z0-9])(?![A-Za-z0-9_/\\-]|\.[A-Za-z0-9])")),
    ("ACCOUNT", re.compile(
        r"(?i)(?:(?<![\w.-])(?:medical[ _-]+record(?:[ _-]+" + _NUMBER_LABEL + r")?"
        r"|health[ _-]+plan[ _-]+beneficiary(?:[ _-]+" + _NUMBER_LABEL + r")?|mrn)(?![\w-])"
        r"|银行账号|银行帐号|银行账户|銀行帳號|病历号|病歷號|医保号|醫保號)"
        + _FIELD_JOIN + r"([A-Z0-9][A-Z0-9-]{4,32}[A-Z0-9])(?![A-Za-z0-9_/\\-]|\.[A-Za-z0-9])")),
    ("ID", re.compile(
        r"(?i)(?:(?<![\w.-])(?:national[ _-]+(?:id|identification)(?:[ _-]+number)?"
        r"|tax(?:payer)?[ _-]+(?:id|identification)(?:[ _-]+number)?"
        r"|licen[cs]e[ _-]+plate(?:[ _-]+number)?"
        r"|vehicle[ _-]+identification[ _-]+number|vin)(?![\w-])|税号|稅號)"
        + _FIELD_JOIN + r"([A-Z0-9][A-Z0-9-]{4,23}[A-Z0-9])(?![A-Za-z0-9_/\\-]|\.[A-Za-z0-9])")),
)


def named_identifier_spans(text):
    for kind, pattern in _NAMED_IDENTIFIERS:
        for match in pattern.finditer(text):
            value = match[1]
            digits = sum(c.isdigit() for c in value)
            # Do not treat measurements, placeholders or repeated dummy digits
            # as identities, nor mask just a prefix of a longer identifier.
            if digits < 5 or len(set(value.replace("-", ""))) < 2:
                continue
            if _UNITS.match(text[match.end(1):]):
                continue
            yield *match.span(1), kind

_SECRET_PREFIX = re.compile(
    r"(?i)(?P<name>(?<![A-Za-z0-9_])(?:[A-Za-z_][\w.-]*[_-])?(?:password|passwd|pwd|passphrase|api[ _-]?key|client[ _-]?secret|access[ _-]?(?:token|key)|private[ _-]?key|pre[ _-]?shared[ _-]?key|secret|token|otp|pin(?:[ _-]?code)?|cvv2?|cvc2?|security[ _-]?code|(?:two[ -]factor[ -])?authentication[ _-]?code|verification[ _-]?code)(?![A-Za-z0-9_])"
    r"|密码|密碼|口令|验证码|恢复码|恢复碼|安全码|安全碼|PIN码|PIN碼|密钥|密鑰|秘钥|私钥|授权码|令牌)"
    r"[ \t\"']*(?P<sep>[:：=]|\bis\b|\bwas\b|是|为|為)[ \t]*(?:[:：][ \t]*)?")
_QUOTES = {'"': '"', "'": "'", '`': '`', '“': '”', '「': '」', '『': '』'}
_NONVALUES = {"expired", "invalid", "required", "empty", "null", "none", "true", "false", "undefined", "redacted", "value", "string", "过期", "错误", "错误的", "空", "为空", "已过期"}
_UNITS = re.compile(r"(?i)^[ \t]+(?:bits?\b|bytes?\b|ms\b|seconds?\b|characters?\b|位|字节|字符|毫秒|秒|分钟|小时)")
_SHORT_CODE = re.compile(r"(?i)(?:(?<![A-Za-z0-9_])(?:pin(?:[ _-]?code)?|cvv2?|cvc2?|otp)(?![A-Za-z0-9_])|安全码|安全碼|验证码|PIN码|PIN碼)[ \t]+(\d{3,8})(?![A-Za-z0-9_])")


def secret_value_allowed(text, start, end, *, literal=False):
    value = text[start:end]
    return (bool(value) and (literal or value.lower() not in _NONVALUES)
            and (literal or not re.fullmatch(r"[*•xX]+", value))
            and not value.startswith(("$", "{{", "[REDACTED]"))
            and not (value.isdigit() and _UNITS.match(text[end:])))


def named_secret_spans(text):
    for match in _SECRET_PREFIX.finditer(text):
        a = match.end()
        if a >= len(text):
            continue
        quote = _QUOTES.get(text[a])
        if quote:
            a += 1
            b = a
            while b < len(text) and text[b] not in "\r\n":
                if text[b] == quote:
                    break
                b += 2 if text[b] == "\\" else 1
            if b >= len(text) or text[b] != quote:
                continue
        else:
            value = re.match(r"[^\s\"'`“”「」,;，。；！？<>\[\]{}]{1,256}", text[a:])
            if value is None:
                continue
            b = a + len(value[0].rstrip("."))
        value = text[a:b]
        # A notification can say "verification code is sent/optional". Those
        # new code labels do not justify masking arbitrary prose as a password.
        if re.search(r"(?:authentication|verification)[ _-]?code$", match["name"], re.I):
            if not re.fullmatch(r"[A-Za-z0-9-]{4,12}", value) or not any(c.isdigit() for c in value):
                continue
        if not secret_value_allowed(text, a, b, literal=bool(quote)):
            continue
        if not quote and re.search(r"[\u3400-\u9fff]", value) and value.startswith(("请输入", "输入", "至少", "需要", "不能", "不应")):
            continue
        yield a, b, "SECRET"


def iban_spans(text):
    for match in _IBAN.finditer(text):
        a = match.start()
        size = IBAN_LENGTHS.get(text[a:a+2])
        if size is None:
            continue
        b, chars = a, []
        while b < match.end() and len(chars) < size:
            if text[b] != " ":
                chars.append(text[b])
            b += 1
        if len(chars) != size or (b < len(text) and text[b].isascii() and text[b].isalnum()):
            continue
        value = "".join(chars)
        digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in value[4:] + value[:4])
        if int(digits) % 97 == 1:
            yield a, b, "ACCOUNT"


def ssn_valid(value):
    v = re.sub(r"[ -]", "", value)
    return len(v) == 9 and v.isascii() and v.isdigit() and v[:3] not in {"000", "666"} and v[0] != '9' and v[3:5] != '00' and v[5:] != '0000' and len(set(v)) > 1


def chinese_id_valid(value):
    if not re.fullmatch(r"[1-9]\d{16}[\dXx]", value):
        return False
    try:
        date(int(value[6:10]), int(value[10:12]), int(value[12:14]))
    except ValueError:
        return False
    weights = (7,9,10,5,8,4,2,1,6,3,7,9,10,5,8,4,2)
    return value[-1].upper() == "10X98765432"[sum(int(v)*w for v,w in zip(value[:17], weights)) % 11]


def contextual_spans(text):
    yield from chinese_identifier_spans(text)
    yield from named_secret_spans(text)
    yield from named_identifier_spans(text)
    for match in _SHORT_CODE.finditer(text):
        if secret_value_allowed(text, *match.span(1)):
            yield *match.span(1), "SECRET"
    yield from iban_spans(text)
    for pattern, group in ((_SSN, 0), (_NAMED_SSN, 1)):
        for match in pattern.finditer(text):
            if ssn_valid(match[group]):
                yield *match.span(group), "ID"
    for pattern in (_PASSPORT, _DRIVER):
        for match in pattern.finditer(text):
            if sum(c.isdigit() for c in match[1]) >= 5 and len(set(match[1])) > 1:
                yield *match.span(1), "ID"
    for match in _PHONE.finditer(text):
        value = match[1]
        if 7 <= sum(c.isdigit() for c in value) <= 21 and not re.fullmatch(r"\d+(?:\.\d+)+", value):
            yield *match.span(1), "PHONE"


_SURNAMES = "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付傅方白邹孟熊秦邱江尹薛段雷侯龙史陶黎贺顾毛郝龚邵万钱严武戴莫孔向汤常温康施文牛樊葛安齐易乔伍庞颜倪庄章岳殷申欧关俞柳包宁舒梅童单裴成谷盛路游辛管蒙华解尤阳司古简车项景费冷席卫柏宗佟应边姬仇商沙荣郎池查"
_COMPOUND = ("欧阳", "司马", "上官", "诸葛", "东方", "皇甫", "尉迟", "公孙", "慕容", "令狐", "司徒", "夏侯", "长孙", "宇文", "轩辕", "端木", "独孤", "南宫", "西门", "钟离")
_ZH_WORDS = {"程序", "文件", "文档", "文案", "文本", "方案", "方法", "方向", "安全", "安装", "管理", "项目", "任务", "申请", "时间", "用户名", "客户", "顾客", "顾问", "成员", "关键", "关心", "查询", "查看", "权限", "接口", "字段", "颜色", "高效", "高级", "张量", "王国", "张开", "常量", "常用", "公司", "部门", "检查", "测试", "系统", "单位", "员工"}
_ZH_WORDS.update({"方式", "方便", "方面", "方程", "方框", "方针", "方块", "文字", "文章", "文化", "文学", "文明", "文献", "文法", "文具", "高度", "高速", "高亮", "高手", "高兴", "金额", "金融", "周末", "周期", "周围", "温度", "余额", "安排", "钱包", "完成", "成功", "成为", "应用", "应该", "单元", "单独", "路径", "路由", "关键", "关注", "关联", "关掉", "包括", "包含", "关于", "关系", "关闭", "连接", "解决", "解析", "解释", "解码", "段落", "章节", "单词", "单个", "包装", "包名", "时区", "时长", "时段", "时刻", "季度", "严格", "严重", "麻烦", "常规", "常数", "边框", "边界", "边缘", "付费", "付款", "路线", "简洁", "简历", "简化", "范例", "商品", "商店", "商业", "左边", "焦点", "毕竟", "尚未", "连续", "连线", "谢谢", "房间", "高端", "古代", "农业", "司法", "施工", "卫生", "荣誉", "宗旨", "齐全", "凌晨", "冷却", "曲线", "白名", "符号", "费用", "纪律", "席位", "管道", "余下", "罗列", "程式", "叶片", "巩固", "迟到", "向导", "易懂", "马虎", "任职", "董事", "边距", "边栏", "尚需"})
_ZH_NAME = re.compile(
    r"(?:联系人|申请人|审批人|负责人|收件人|发件人|姓名|名字|提交者|同事|客户|我叫|他叫|她叫|我是|通知|抄送|联系|请核对)"
    r"[ \t]*(?:[:：]|是|为)?[ \t]*([\u4e00-\u9fff]{2,4})"
    r"(?=$|[\s,，。;；:：！？、\"”'<>]|的|先生|女士|老师|提交|反馈|负责|今天|昨天|需要|已经)")


def chinese_name_spans(text):
    """Like Magpie zhNameSpans: surname + explicit person cue, never a bare surname."""
    for match in _ZH_NAME.finditer(text):
        value = match[1]
        family = next((s for s in _COMPOUND if value.startswith(s)), value[0])
        if family not in _COMPOUND and family not in _SURNAMES:
            continue
        if not 1 <= len(value) - len(family) <= 2 or any(value.startswith(w) for w in _ZH_WORDS):
            continue
        yield *match.span(1), "PERSON"
