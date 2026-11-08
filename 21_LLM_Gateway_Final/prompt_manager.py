"""SQLite 模板版本存储，复用单次变量替换。"""

import re
import sqlite3
from contextlib import closing
from pathlib import Path


DEFAULT_TEMPLATES: dict[str, str] = {
    "translate": "请把 {{text}} 翻译成{{language}}",
    "summarize": "请总结以下内容：\n{{text}}",
}

_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


DB_PATH = Path(__file__).resolve().with_name("prompts.db")


def init_db() -> None:
    """创建表并幂等写入默认模板；不覆盖已有内容。"""
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as connection:
        with connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS prompt_templates (
                    template_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    content TEXT NOT NULL,
                    PRIMARY KEY (template_id, version)
                )
            """)
            connection.executemany(
                "INSERT OR IGNORE INTO prompt_templates VALUES (?, 1, ?)",
                DEFAULT_TEMPLATES.items(),
            )


def save_template(template_id: str, content: str) -> int:
    """保存新版本；即使内容相同也增加版本号。"""
    if not isinstance(template_id, str) or not template_id.strip():
        raise ValueError("template_id 必须是非空白字符串")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content 必须是非空白字符串")
    init_db()
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM prompt_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()[0]
            version = latest + 1
            connection.execute(
                "INSERT INTO prompt_templates (template_id, version, content) VALUES (?, ?, ?)",
                (template_id, version, content),
            )
    return version


def get_template(template_id: str, version: int | None = None) -> str:
    """指定版本精确查询，省略版本则返回最新版。"""
    if version is not None and (type(version) is not int or version < 1):
        raise ValueError("模板版本必须是大于等于 1 的整数")
    init_db()
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as connection:
        if version is None:
            row = connection.execute(
                "SELECT content FROM prompt_templates WHERE template_id = ? ORDER BY version DESC LIMIT 1",
                (template_id,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT content FROM prompt_templates WHERE template_id = ? AND version = ?",
                (template_id, version),
            ).fetchone()
    if row is None:
        if version is None:
            raise ValueError(f"模板 ID 不存在：{template_id}")
        raise ValueError(f"模板或版本不存在：{template_id}，版本 {version}")
    return row[0]


def render_prompt(template_id: str, variables: dict[str, str], version: int | None = None) -> str:
    """额外变量忽略；缺少变量报错；替换值按原文插入且不递归渲染。"""
    template = get_template(template_id, version)
    required = set(_VARIABLE.findall(template))
    missing = required - variables.keys()
    if missing:
        raise ValueError("缺少模板变量：" + ", ".join(sorted(missing)))
    if any(not isinstance(value, str) for value in variables.values()):
        raise ValueError("模板变量值必须是字符串")
    # 回调返回值不会被当作正则替换表达式，也不会再次扫描其中的占位符。
    return _VARIABLE.sub(lambda match: variables[match.group(1)], template)
