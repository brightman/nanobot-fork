---
name: xiaohongshu-post
description: Create Xiaohongshu post drafts (title, body, hashtags, CTA, publish checklist) for product marketing, personal branding, and campaign content.
---

# Xiaohongshu Post Skill

Use this skill when the user asks to create or optimize 小红书发帖内容, including:
- Topic ideas and content plan
- Post title/body/hashtag writing
- Conversion-oriented CTA design
- Publish checklist and compliance self-check

## Workflow

1. Clarify missing inputs quickly:
- Product/service
- Target audience
- Post goal (awareness, lead, conversion, recruitment)
- Tone (professional, friendly, story-driven, data-driven)

2. Generate a draft with this structure:
- `标题` (short and specific)
- `正文` (hook -> value points -> proof -> CTA)
- `标签` (5-10 relevant hashtags)
- `封面文案建议` (1-2 lines)
- `发布检查清单` (brand consistency, factual claims, prohibited terms)

3. Use the bundled script for deterministic output when needed.

## Script

Use:

```bash
python3 scripts/create_post.py \
  --topic "会议记录效率提升" \
  --product "nanobot" \
  --audience "中小团队负责人" \
  --goal "获客" \
  --tone "专业可信" \
  --cta "私信领取演示方案" \
  --keywords "AI助手,自动化,效率工具"
```

Options:
- `--workspace`: base output directory (default: current directory)
- `--out`: explicit output path

Output:
- Markdown draft file under `marketing/xiaohongshu/`

## Auto Post with Playwright

When user asks to post to Xiaohongshu automatically, use:

```bash
python3 scripts/post_with_playwright.py \
  --post-file marketing/xiaohongshu/20260227-ai销售助手落地.md \
  --login
```

Then run actual post:

```bash
python3 scripts/post_with_playwright.py \
  --post-file marketing/xiaohongshu/20260227-ai销售助手落地.md \
  --publish
```

Notes:
- First run should be headed and login manually.
- Session is saved to `~/.nanobot/workspace/.auth/xiaohongshu_state.json`.
- If selectors fail due to UI changes, rerun with `--debug` and adjust selectors in script.

## Safety Rules

- Do not fabricate customer cases, data, or certifications.
- Do not include absolute claims like "100%有效" or medical/financial guarantees.
- Keep promotional language natural; prioritize practical value and credibility.
