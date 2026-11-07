"""内存模板：只查找、校验和单次替换，不调用模型或执行表达式。"""

import re


TEMPLATES: dict[str, str] = {
    "translate": "请把 {{text}} 翻译成{{language}}",
    "summarize": "请总结以下内容：\n{{text}}",
}

_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render_prompt(template_id: str, variables: dict[str, str]) -> str:
    """额外变量忽略；缺少变量报错；替换值按原文插入且不递归渲染。"""
    if template_id not in TEMPLATES:
        raise ValueError(f"模板 ID 不存在：{template_id}")
    template = TEMPLATES[template_id]
    required = set(_VARIABLE.findall(template))
    missing = required - variables.keys()
    if missing:
        raise ValueError("缺少模板变量：" + ", ".join(sorted(missing)))
    if any(not isinstance(value, str) for value in variables.values()):
        raise ValueError("模板变量值必须是字符串")
    # 回调返回值不会被当作正则替换表达式，也不会再次扫描其中的占位符。
    return _VARIABLE.sub(lambda match: variables[match.group(1)], template)
