# WeChat 公众号草稿接口说明

本 skill 只负责**保存草稿**，不调用发布接口。用户登录
mp.weixin.qq.com 后手动检查并发布。

## 使用方式

本地生成审阅文件（不需要任何密钥）：

    .\.venv\Scripts\python.exe scripts\generate_wechat_draft.py "G:\codex\每周文献整理\ZoteroWeeklySync\archive\20260814\Zotero_本周入库清单_20260814.json" --summaries examples\summaries_20260814.json --out-dir wechat_draft

保存到公众号草稿箱（需要真实配置）：

    .\.venv\Scripts\python.exe scripts\generate_wechat_draft.py "G:\codex\每周文献整理\ZoteroWeeklySync\archive\20260814\Zotero_本周入库清单_20260814.json" --config ZoteroWeeklySync\wechat_mp_config.json --summaries examples\summaries_20260814.json --upload

注意：脚本不会调用 freepublish/submit，所以不会自动发布。

## 个人号 / 未认证号限制

- 公开文档中的 draft/add 面向公众号和服务号；**个人订阅号、未认证号**
  很可能返回 48001（api 功能未授权）。
- 如果遇到 48001，不要重试。脚本已经生成本地 HTML，直接复制
  wechat_draft_YYYYMMDD.content.html 内容，粘贴到公众号编辑器保存草稿。

## 配置

复制 assets/wechat_mp_config.template.json 为真实配置，填好：

- appid：公众号 AppID。
- secret：公众号 AppSecret，必须保密，不要提交到 Git。
- author：作者名，最大 16 字符。
- title：标题前缀，最终标题为 前缀 | 日期，最大 32 字符。
- digest：摘要，最大 120 字符。
- content_source_url：可选，阅读原文链接。
- need_open_comment / only_fans_can_comment：0/1 开关。
- thumb_media_id：永久封面素材 ID，见下文。
- api_timeout_seconds：HTTP 超时时间。

## access_token

    GET https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=APPID&secret=APPSECRET

返回 JSON 中的 access_token 默认 2 小时有效。脚本每次运行都会重新获取，
不需要缓存。

## draft/add

    POST https://api.weixin.qq.com/cgi-bin/draft/add?access_token=ACCESS_TOKEN

请求体：

    {
      "articles": [
        {
          "title": "每周疲劳寿命文献更新 | 2026-08-14",
          "author": "疲劳寿命文献周报",
          "digest": "...",
          "content": "<section>...</section>",
          "content_source_url": "",
          "thumb_media_id": "PERMANENT_MEDIA_ID",
          "need_open_comment": 0,
          "only_fans_can_comment": 0
        }
      ]
    }

成功返回包含 media_id，登录公众号后台可在草稿箱看到。

## 字段限制

- title：最大 32 字符。
- author：最大 16 字符。
- digest：最大 120 字符。
- content：HTML 片段；脚本会在超过 20000 字符时告警。
- 内容中的 JS 会被公众号过滤，外部图片 URL 也不会生效。

## 图片与封面

- content 里不要使用外部图片 URL。需要使用 upload_news_image 或永久素材
  接口上传后返回的 mmbiz.qpic.cn 图片地址。
- thumb_media_id 是**永久素材 media_id**，不是临时素材。获取方式：
  1. 调用
     POST https://api.weixin.qq.com/cgi-bin/material/add_material?access_token=ACCESS_TOKEN&type=thumb
     上传封面图，返回 media_id；
  2. 把返回的 media_id 填入 wechat_mp_config.json 的 thumb_media_id。
- 个人号如果无法使用素材接口，就手动在公众号编辑器里设置封面，脚本仍可
  生成正文。

## IP 白名单

公众号后台「设置与开发 → 基本配置」里，AppSecret 只在配置了 IP 白名单的
服务器上可用。本机 IP 如果不是固定公网 IP，可能需要：

- 把本机当前公网 IP 加入白名单后运行；
- 或者本地生成草稿 HTML，再手动粘贴，避免依赖 AppSecret 调用。

## 安全红线

- 绝不提交 wechat_mp_config.json、AppSecret、access_token 到 Git。
- 绝不调用 freepublish/submit 或其他发布接口。
- 不绕过登录、认证或 API 授权限制。
- 生成的中文摘要/点评必须人工复核，不能当作论文原文摘要。
