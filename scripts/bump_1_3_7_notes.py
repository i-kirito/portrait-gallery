#!/usr/bin/env python3
from pathlib import Path

p = Path("README.md")
t = p.read_text()

old = "### v1.3.7\n\n- 全部画廊改为滚动接近底部时自动加载下一页"
new = "### v1.3.6\n\n- 全部画廊改为滚动接近底部时自动加载下一页"
if old in t:
    t = t.replace(old, new, 1)
    print("restored v1.3.6 history header")

marker = None
for m in ("## 🧾 Release Notes\n\n", "## Release Notes\n\n"):
    if m in t:
        marker = m
        break
if marker is None:
    raise SystemExit("release notes marker missing")

notes = (
    "### v1.3.7\n\n"
    "- 自定义生图支持有序多参考图：第一张锁动作/穿搭/场景，后续参考只迁脸与发色；API/UI 均可传 ref_images。\n"
    "- 双参考上游失败自动降级为 face-only；multi-ref 对 timeout / Codex images/edits EOF 快速失败，日志带 kind 与 refs。\n"
    "- 自定义生图轻量 prompt 路径：场景优先，避免过长 quality 堆叠导致崩图；日程 photo_style_en 由 LLM 按当日氛围判断摄影语言。\n"
    "- 「现在生图」受每日计划配额约束；重抽改为替换原卡片并保留历史版本，支持版本回退。\n"
    "- 衣柜衣架图支持历史版本归档/切换；参考像标签改为悬停缩略图，不再展示冗长文件名。\n"
    "- 日程生成加强近期动作去重（LLM 判断近 3 天），可出现双人互动日程，但生图强制单主体入镜。\n"
    "- 图片版本激活、翻译超时与 GPT Image 失败分类（moderation/timeout/EOF）更稳健。\n\n"
)

head, rest = t.split(marker, 1)
if not rest.lstrip().startswith("### v1.3.7"):
    t = head + marker + notes + rest
    print("inserted v1.3.7 notes")
else:
    print("v1.3.7 notes already present")

p.write_text(t)
print("VERSION", Path("VERSION").read_text().strip())
print("counts 1.3.6", t.count("1.3.6"), "1.3.7", t.count("1.3.7"))
