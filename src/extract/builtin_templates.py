from __future__ import annotations

from src.domain.schemas import ColumnSpec, FieldGroup, FieldRegion, LineRules

from .prompt_builder import PromptTemplate


BUILTIN_TEMPLATES = {
    "sigcard": PromptTemplate(
        name="名片抽取",
        description=(
            "从用户提供的名片、邮件签名或联系人信息文本中提取结构化字段。\n"
            "输出顺序必须为：姓名、职位、公司名称、电话号码、手机号码、邮箱地址、公司地址。\n"
            "若字段不存在，使用单个空格字符串 \" \" 占位。\n"
            "同一段文本可能包含多个人的名片信息，应逐人输出一行。\n"
            "不要把多个人的字段拼接到同一行；同一列若存在多个候选值，只保留当前这个人的对应值。"
        ),
        examples=[
            ["姓名", "职位", "公司名称", "电话号码", "手机号码", "邮箱地址", "公司地址"],
            ["张三", "产品经理", "Dify 公司", "+86 23-40768368", "13888889999", "zhangsan@dify.ai", "北京市海淀区中关村大街1号"],
        ],
        columns=(
            ColumnSpec(name="姓名"),
            ColumnSpec(name="职位"),
            ColumnSpec(name="公司名称", type="company"),
            ColumnSpec(name="电话号码", type="phone"),
            ColumnSpec(name="手机号码", type="phone"),
            ColumnSpec(name="邮箱地址", type="email"),
            ColumnSpec(name="公司地址"),
        ),
    ),
    "invoice": PromptTemplate(
        name="发票抽取",
        description=(
            "从用户提供的发票 OCR 文本中提取结构化字段。\n"
            "输出顺序必须为：发票号码、开票日期、购买方名称、购买方纳税人识别号、购买方地址电话、货物或应税劳务服务名称、金额、税率、销售方名称、销售方纳税人识别号、销售方地址电话、销售方开户行及账号。\n"
            "同一段文本中可能包含多张发票，应逐张输出多行。\n"
            "每条商品或服务明细必须一条明细一行；同一张发票有多条明细时，重复该发票的其他公共字段。\n"
            "不要用空格拼接多条商品明细到同一个单元格；若原文有多条明细，不允许合并成一条记录。\n"
            "金额统一输出未带货币符号的数字字符串，税率保留百分号，缺失字段使用单个空格字符串 \" \" 占位。"
        ),
        examples=[
            [
                "发票号码",
                "开票日期",
                "购买方名称",
                "购买方纳税人识别号",
                "购买方地址电话",
                "货物或应税劳务服务名称",
                "金额",
                "税率",
                "销售方名称",
                "销售方纳税人识别号",
                "销售方地址电话",
                "销售方开户行及账号",
            ],
            [
                "56115415",
                "2023年07月03日",
                "塔塔塔塔气体有限公司",
                "915000002312322133",
                "重庆市长寿区一二三路237号 023-12345678",
                "*物流辅助服务*收派服务费",
                "211.33",
                "6%",
                "顺丰速运重庆有限公司",
                "91500105691454217L",
                "重庆市江北区庆云路1号国金中心T1办公楼16楼 023-34535345",
                "中国工商银行股份有限公司重庆石子山支行3100084929536435452",
            ],
        ],
        columns=(
            ColumnSpec(name="发票号码"),
            ColumnSpec(name="开票日期", type="date", date_formats=("%Y年%m月%d日", "%Y-%m-%d")),
            ColumnSpec(name="购买方名称"),
            ColumnSpec(name="购买方纳税人识别号"),
            ColumnSpec(name="购买方地址电话"),
            ColumnSpec(name="货物或应税劳务服务名称"),
            ColumnSpec(name="金额", type="number", thousands_separator=","),
            ColumnSpec(name="税率"),
            ColumnSpec(name="销售方名称"),
            ColumnSpec(name="销售方纳税人识别号"),
            ColumnSpec(name="销售方地址电话"),
            ColumnSpec(name="销售方开户行及账号"),
        ),
        line_rules=LineRules(
            start=r"货物或应税劳务(?:服务)?名称",
            end=r"合\s*计|价税合计",
            line=(
                r"^\*?(?P<货物或应税劳务服务名称>\S+)"
                r"(?:\s+\S+){0,3}"
                r"\s+(?P<金额>\d[\d,]*\.\d{2})"
                r"\s+(?P<税率>\d+%)"
                r"(?:\s+\d[\d,]*\.\d{2})?$"
            ),
            skip_line=r"^规格型号|^项目|^数量|^单价|^税额",
            repeating_field_from_parent=(
                "发票号码",
                "开票日期",
                "购买方名称",
                "购买方纳税人识别号",
                "购买方地址电话",
                "销售方名称",
                "销售方纳税人识别号",
                "销售方地址电话",
                "销售方开户行及账号",
            ),
        ),
        field_regions=(
            FieldRegion(field_name="发票号码", left=0.67, top=0.12, right=0.95, bottom=0.19),
            FieldRegion(field_name="开票日期", left=0.67, top=0.17, right=0.95, bottom=0.24),
            FieldRegion(field_name="购买方纳税人识别号", left=0.18, top=0.26, right=0.64, bottom=0.34),
            FieldRegion(field_name="销售方纳税人识别号", left=0.18, top=0.74, right=0.64, bottom=0.82),
        ),
        field_groups=(
            FieldGroup(
                name="购买方",
                field_names=("购买方名称", "购买方纳税人识别号", "购买方地址电话"),
                anchor_keywords=("购买方信息", "购买方"),
            ),
            FieldGroup(
                name="销售方",
                field_names=("销售方名称", "销售方纳税人识别号", "销售方地址电话", "销售方开户行及账号"),
                anchor_keywords=("销售方信息", "销售方"),
            ),
        ),
        exclusive_group_pairs=(("购买方", "销售方"),),
    ),
}
