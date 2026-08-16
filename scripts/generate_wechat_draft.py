#!/usr/bin/env python3
"""Generate a WeChat Official Account draft from an archived weekly batch.

This script is intentionally publish-free. It produces local review files and,
only when requested with --upload, calls the Official Account draft/add API so
the user can log into mp.weixin.qq.com and manually publish the saved draft.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
DRAFT_ADD_URL = "https://api.weixin.qq.com/cgi-bin/draft/add"
USER_AGENT = "weekly-fatigue-literature-update/1.0"

DEFAULT_TITLE = "每周疲劳寿命文献更新"
DEFAULT_AUTHOR = "疲劳寿命文献周报"
DEFAULT_DIGEST = "本周新增疲劳寿命相关文献速览与点评，供人工复核后发布。"

CATEGORY_NOTES = {
    1: "围绕物理约束与机器学习相结合的疲劳寿命预测，强调机理或物理信息对模型泛化与可信度的支撑。",
    2: "聚焦疲劳损伤演化与寿命预测模型，通常涉及损伤累积、寿命模型及其工程应用。",
    3: "关注轨道车辆构架或部件的疲劳、载荷谱与实测服役数据，为服役载荷与寿命评估提供依据。",
    4: "面向加速度或状态监测驱动的间接疲劳预测，利用实测动态响应推断载荷或损伤状态。",
    5: "涉及多轴疲劳、焊接接头、断裂与裂纹扩展等局部失效机制。",
    6: "聚焦多保真、迁移学习、不确定性与小样本条件下的疲劳建模。",
}

PDF_STATUS_TEXT = {
    "available": "本批提供可下载 PDF",
    "requires_access": "全文需机构访问或购买",
    "unavailable": "未找到合法开放 PDF",
}


class DraftApiError(RuntimeError):
    """Raised when the WeChat API returns a non-zero error code."""


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def coerce_records(raw: Any, source: Path) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        records = raw
    elif isinstance(raw, dict):
        records = []
        for key in ("records", "items", "data", "papers", "articles"):
            value = raw.get(key)
            if isinstance(value, list):
                records = value
                break
    else:
        records = []
    if not records:
        raise SystemExit(f"批量文件没有可用记录：{source}")
    if not all(isinstance(item, dict) for item in records):
        raise SystemExit(f"批量记录格式异常：{source}")
    return records


def normalize_summaries(raw: Any) -> Dict[str, Dict[str, str]]:
    result: Dict[str, Dict[str, str]] = {}
    if not raw:
        return result
    entries: List[Any] = []
    if isinstance(raw, dict) and isinstance(raw.get("records"), list):
        entries = raw["records"]
    elif isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        entries = [{"id": key, **value} for key, value in raw.items()]

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ident = entry.get("cumulative_id") or entry.get("id") or entry.get("ref")
        if ident is None:
            continue
        if isinstance(entry, dict):
            summary = str(entry.get("summary") or entry.get("abstract") or "")
            comment = str(entry.get("comment") or entry.get("review") or entry.get("点评") or "")
        elif isinstance(entry, str):
            summary = entry
            comment = ""
        else:
            continue
        result[str(ident)] = {"summary": summary.strip(), "comment": comment.strip()}
    return result


def load_summaries(path: Optional[str]) -> Dict[str, Dict[str, str]]:
    if not path:
        return {}
    return normalize_summaries(read_json(path))


def extract_date(records: List[Dict[str, Any]], batch_path: Path) -> tuple[str, str]:
    for record in records:
        value = str(record.get("source_week") or "").strip()
        match = re.match(r"^\d{4}-\d{2}-\d{2}$", value)
        if match:
            date = match.group(0)
            return date, date.replace("-", "")
    match = re.search(r"(\d{8})", batch_path.name)
    if match:
        digits = match.group(1)
        date = f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
        return date, digits
    date = _dt.date.today().isoformat()
    return date, date.replace("-", "")


def clean_text(value: Any) -> str:
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value if str(item).strip())
    return html.escape(str(value or "").strip())


def clean_url(value: Any) -> str:
    return html.escape(str(value or "").strip(), quote=True)


def truncate(value: str, limit: int) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def format_authors(record: Dict[str, Any]) -> str:
    authors = record.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    authors = [str(item).strip() for item in authors if str(item).strip()]
    if not authors:
        return ""
    if len(authors) <= 3:
        return "、".join(authors)
    return "、".join(authors[:3]) + " 等"


def citation_meta(record: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("journal", "year", "volume", "issue", "pages", "article_number"):
        value = str(record.get(key) or "").strip()
        if value:
            parts.append(value)
    return " · ".join(parts)


def classification_name(record: Dict[str, Any]) -> str:
    target = str(record.get("target_collection") or "").strip()
    if target:
        return target
    category = record.get("main_category")
    return CATEGORY_NOTES.get(category, "").split("，", 1)[0]


def pdf_note(record: Dict[str, Any]) -> str:
    status = str(record.get("pdf_access_status") or "").strip()
    if record.get("pdf_url"):
        return "本批提供可下载 PDF"
    return PDF_STATUS_TEXT.get(status, "PDF 状态待确认")


def fallback_summary(record: Dict[str, Any]) -> str:
    title = str(record.get("title") or "").strip()
    category = classification_name(record)
    note = CATEGORY_NOTES.get(record.get("main_category"), "")
    if note:
        return f"本文属于“{category}”。{note} 本条当前为基于题录信息的自动定位，不含人工全文摘要，请结合全文复核。"
    return f"本文为《{title}》，分类为“{category}”。本条当前缺少人工中文摘要，请结合全文补充。"


def fallback_comment(record: Dict[str, Any]) -> str:
    category = record.get("main_category")
    if category in (3, 4):
        comment = "与本项目关注的轨道车辆构架疲劳、实测载荷/监测路线相关性较强，建议重点跟踪。"
    else:
        comment = "可作为方法论或案例参考，具体价值需结合全文判断。"
    if pdf_note(record) in ("全文需机构访问或购买", "未找到合法开放 PDF"):
        comment += " 本批未提供合法开放全文，精读前请先解决全文获取。"
    return comment


def resolved_summary(record: Dict[str, Any], ref: str, summaries: Dict[str, Dict[str, str]]) -> tuple[str, str]:
    entry = summaries.get(ref) or summaries.get(str(record.get("cumulative_id") or "")) or {}
    summary = entry.get("summary") or fallback_summary(record)
    comment = entry.get("comment") or fallback_comment(record)
    return summary, comment


def build_content_fragment(records: List[Dict[str, Any]], summaries: Dict[str, Dict[str, str]], date: str) -> str:
    blocks: List[str] = []
    blocks.append(
        '<section style="font-size:15px;line-height:1.8;color:#222;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif;">'
    )
    blocks.append(f'<h2 style="font-size:21px;font-weight:bold;color:#1f3864;margin:0 0 6px;">{clean_text(DEFAULT_TITLE)} | {clean_text(date)}</h2>')
    blocks.append(
        f'<p style="font-size:13px;color:#888;margin:0 0 14px;">共 {len(records)} 篇 · 自动生成审阅稿 · 不自动发布</p>'
    )

    for index, record in enumerate(records, start=1):
        ref = str(record.get("cumulative_id") or index)
        summary, comment = resolved_summary(record, ref, summaries)
        title = clean_text(record.get("title"))
        authors = format_authors(record)
        meta = citation_meta(record)
        category = clean_text(classification_name(record))
        doi = str(record.get("doi") or "").strip()
        url = str(record.get("url") or "").strip()
        pdf_text = pdf_note(record)

        blocks.append('<section style="margin:20px 0;padding-top:14px;border-top:1px solid #e3e6ec;">')
        blocks.append(f'<h3 style="font-size:17px;font-weight:bold;color:#17375e;margin:0 0 6px;">{index}. {title}</h3>')
        meta_line = authors or ""
        if meta:
            meta_line = f"{meta_line} · {meta}" if meta_line else meta
        blocks.append(f'<p style="font-size:13px;color:#667;">{meta_line}</p>')
        blocks.append(f'<p style="font-size:13px;color:#667;">分类：{category} · {clean_text(pdf_text)}</p>')
        blocks.append(f'<p style="margin:10px 0 6px;"><strong style="color:#1f3864;">中文摘要：</strong>{clean_text(summary)}</p>')
        blocks.append(f'<p style="margin:0 0 10px;"><strong style="color:#b26b00;">点评：</strong>{clean_text(comment)}</p>')

        link_bits: List[str] = []
        if doi:
            link_bits.append(f'DOI：<a href="https://doi.org/{clean_url(doi)}" style="color:#337ab7;">{clean_text(doi)}</a>')
        if url:
            link_bits.append(f'原文：<a href="{clean_url(url)}" style="color:#337ab7;">出版社页面</a>')
        if link_bits:
            blocks.append(f'<p style="font-size:12px;color:#888;">{" · ".join(link_bits)}</p>')
        blocks.append("</section>")

    blocks.append("</section>")
    return "\n".join(blocks)


def build_markdown(records: List[Dict[str, Any]], summaries: Dict[str, Dict[str, str]], date: str) -> str:
    lines: List[str] = [f"# {DEFAULT_TITLE} | {date}", "", f"> 共 {len(records)} 篇 · 自动生成审阅稿 · 不自动发布", ""]
    for index, record in enumerate(records, start=1):
        ref = str(record.get("cumulative_id") or index)
        summary, comment = resolved_summary(record, ref, summaries)
        title = str(record.get("title") or "").strip()
        authors = format_authors(record)
        meta = citation_meta(record)
        category = classification_name(record)
        doi = str(record.get("doi") or "").strip()
        url = str(record.get("url") or "").strip()

        lines.append(f"## {index}. {title}")
        lines.append("")
        if authors:
            lines.append(f"- 作者：{authors}")
        if meta:
            lines.append(f"- 题录：{meta}")
        lines.append(f"- 分类：{category}")
        lines.append(f"- 全文：{pdf_note(record)}")
        if doi:
            lines.append(f"- DOI：https://doi.org/{doi}")
        if url:
            lines.append(f"- 原文：{url}")
        lines.append("")
        lines.append(f"**中文摘要**：{summary}")
        lines.append("")
        lines.append(f"**点评**：{comment}")
        lines.append("")
    return "\n".join(lines)


def build_standalone_html(fragment: str, date: str) -> str:
    return (
        '<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{DEFAULT_TITLE} | {date}</title>\n"
        '<style>body{max-width:720px;margin:24px auto;padding:0 16px;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,sans-serif;}\n'
        ".mp-content{font-size:15px;line-height:1.8;color:#222;}\n"
        "footer{margin-top:28px;color:#999;font-size:12px;}</style>\n"
        "</head>\n<body>\n<article class=\"mp-content\">\n"
        f"{fragment}\n</article>\n<footer>由 weekly-fatigue-literature-update 生成 · 仅供人工审阅 · 不自动发布</footer>\n</body>\n</html>\n"
    )


def build_draft_payload(
    records: List[Dict[str, Any]],
    summaries: Dict[str, Dict[str, str]],
    date: str,
    config: Dict[str, Any],
    fragment: str,
) -> Dict[str, Any]:
    title = truncate(str(config.get("title") or DEFAULT_TITLE), 32)
    title = truncate(f"{title} | {date}", 32)
    author = truncate(str(config.get("author") or DEFAULT_AUTHOR), 16)
    digest = truncate(str(config.get("digest") or DEFAULT_DIGEST), 120)
    article: Dict[str, Any] = {
        "title": title,
        "author": author,
        "digest": digest,
        "content": fragment,
        "content_source_url": str(config.get("content_source_url") or ""),
        "need_open_comment": int(config.get("need_open_comment") or 0),
        "only_fans_can_comment": int(config.get("only_fans_can_comment") or 0),
    }
    thumb_media_id = str(config.get("thumb_media_id") or "").strip()
    if thumb_media_id:
        article["thumb_media_id"] = thumb_media_id
    return {"articles": [article]}


def get_access_token(config: Dict[str, Any]) -> str:
    appid = str(config.get("appid") or "").strip()
    secret = str(config.get("secret") or "").strip()
    if not appid or not secret or appid.startswith("<") or secret.startswith("<"):
        raise DraftApiError("微信配置缺少有效的 appid / secret，无法获取 access_token。")
    params = urllib.parse.urlencode({"grant_type": "client_credential", "appid": appid, "secret": secret})
    url = f"{TOKEN_URL}?{params}"
    timeout = int(config.get("api_timeout_seconds") or 15)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DraftApiError(f"获取 access_token 失败：HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise DraftApiError(f"获取 access_token 失败：网络错误 {exc.reason}") from exc
    token = data.get("access_token")
    if not token:
        raise DraftApiError(f"获取 access_token 失败：{data.get('errcode')} {data.get('errmsg')}")
    return token


def upload_draft(token: str, payload: Dict[str, Any], config: Dict[str, Any]) -> str:
    timeout = int(config.get("api_timeout_seconds") or 15)
    url = f"{DRAFT_ADD_URL}?access_token={urllib.parse.quote(token)}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise DraftApiError(f"draft/add 失败：HTTP {exc.code} {exc.reason} {detail}") from exc
    except urllib.error.URLError as exc:
        raise DraftApiError(f"draft/add 失败：网络错误 {exc.reason}") from exc
    if data.get("errcode") not in (0, None):
        code = data.get("errcode")
        message = data.get("errmsg")
        if code == 48001:
            message = "个人号/未认证号可能无权调用 draft/add（48001）。请改用本地 HTML 手动粘贴到 mp.weixin.qq.com 保存草稿。"
        raise DraftApiError(f"draft/add 返回错误：{code} {message}")
    return str(data.get("media_id") or "")


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a manual-review WeChat Official Account draft from a weekly batch.")
    parser.add_argument("batch_json", help="归档批次 JSON，例如 Zotero_本周入库清单_YYYYMMDD.json")
    parser.add_argument("--config", help="微信草稿配置 JSON（assets/wechat_mp_config.template.json 的副本）")
    parser.add_argument("--summaries", help="可选的中文摘要/点评 JSON，按 cumulative_id 覆盖")
    parser.add_argument("--out-dir", default=None, help="输出目录，默认当前目录下的 wechat_draft")
    parser.add_argument("--upload", action="store_true", help="调用公众号 draft/add 保存草稿；缺省仅生成本地文件")
    parser.add_argument("--title", help="覆盖公众号标题前缀")
    parser.add_argument("--author", help="覆盖公众号作者名")
    args = parser.parse_args(argv)

    batch_path = Path(args.batch_json).resolve()
    if not batch_path.exists():
        print(f"批量文件不存在：{batch_path}", file=sys.stderr)
        return 2

    raw = read_json(str(batch_path))
    records = coerce_records(raw, batch_path)
    date, compact = extract_date(records, batch_path)
    summaries = load_summaries(args.summaries)

    config: Dict[str, Any] = {}
    if args.config:
        config_path = Path(args.config).resolve()
        if not config_path.exists():
            print(f"微信配置文件不存在：{config_path}", file=sys.stderr)
            return 2
        config = read_json(str(config_path))
    if args.title:
        config["title"] = args.title
    if args.author:
        config["author"] = args.author

    fragment = build_content_fragment(records, summaries, date)
    markdown = build_markdown(records, summaries, date)
    standalone = build_standalone_html(fragment, date)
    payload = build_draft_payload(records, summaries, date, config, fragment)

    out_dir = Path(args.out_dir).resolve() if args.out_dir else (Path.cwd() / "wechat_draft").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"wechat_draft_{compact}"
    content_path = base.with_name(base.name + ".content.html")
    preview_path = base.with_name(base.name + ".html")
    markdown_path = base.with_name(base.name + ".md")
    payload_path = base.with_name(base.name + ".payload.json")

    write_text(content_path, fragment)
    write_text(preview_path, standalone)
    write_text(markdown_path, markdown)
    write_text(payload_path, json.dumps(payload, ensure_ascii=False, indent=2))

    print(f"周报日期：{date}")
    print(f"记录数：{len(records)}")
    print(f"内容片段：{content_path}")
    print(f"审阅页面：{preview_path}")
    print(f"Markdown：{markdown_path}")
    print(f"API 载荷：{payload_path}")

    if len(fragment) > 20000:
        print("警告：content 长度超过 20000 字符，公众号编辑器可能截断，请检查。")

    if not args.upload:
        print("未执行 --upload，仅生成本地审阅文件。")
        return 0

    if not str(config.get("thumb_media_id") or "").strip():
        print("警告：thumb_media_id 未配置。公众号 draft/add 通常要求永久封面素材，可能需要先在 references/wechat_draft_api.md 中按说明上传。")

    try:
        token = get_access_token(config)
        media_id = upload_draft(token, payload, config)
        print(f"草稿已保存，media_id={media_id or '(空)'}。请登录 mp.weixin.qq.com 手动检查并发布。")
        return 0
    except DraftApiError as exc:
        print(f"草稿上传失败：{exc}", file=sys.stderr)
        print("本地审阅文件已生成，可手动粘贴到公众号编辑器保存草稿。", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
