#!/usr/bin/env python3
"""Generate a Xiaohongshu post draft markdown file."""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path


def slugify(text: str) -> str:
    raw = re.sub(r"\s+", "-", text.strip().lower())
    raw = re.sub(r"[^a-z0-9\-\u4e00-\u9fff]", "", raw)
    return raw[:40] or "post"


def build_content(args: argparse.Namespace) -> str:
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    hashtags = " ".join(f"#{k.replace(' ', '')}" for k in keywords[:10])
    title = args.title.strip() if args.title else f"{args.topic.strip()}：给{args.audience.strip()}的实战建议"

    body = f"""# 标题
{title}

# 正文
开场：如果你也在为“{args.topic.strip()}”反复投入时间，下面这套方法可以少走弯路。

价值点：
1. 先明确目标：本帖目标是“{args.goal.strip()}”，避免内容只停留在泛分享。
2. 针对人群：围绕“{args.audience.strip()}”最常见问题展开，而不是泛泛而谈。
3. 落地路径：用 `{args.product.strip()}` 先做小范围试跑，再逐步标准化流程。

证明与案例：
- 给出一个真实、可验证的小案例（避免夸大数据）。
- 清楚写出前后变化、实施成本和适用边界。

行动建议：
{args.cta.strip()}

# 标签
{hashtags}

# 封面文案建议
1. {args.topic.strip()}，这样做效率更高
2. 不是更努力，而是方法更对

# 发布检查清单
- [ ] 观点是否具体且可执行
- [ ] 是否避免绝对化承诺和违规词
- [ ] 是否包含清晰 CTA
- [ ] 是否与品牌语气一致（{args.tone.strip()}）
"""
    return body


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create Xiaohongshu post draft.")
    p.add_argument("--topic", required=True)
    p.add_argument("--product", required=True)
    p.add_argument("--audience", required=True)
    p.add_argument("--goal", default="品牌曝光")
    p.add_argument("--tone", default="真诚实用")
    p.add_argument("--cta", default="欢迎评论区交流")
    p.add_argument("--keywords", default="小红书,内容营销,品牌增长")
    p.add_argument("--title", default="")
    p.add_argument("--workspace", default=".")
    p.add_argument("--out", default="")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    content = build_content(args)

    if args.out:
        out_path = Path(args.out).expanduser()
    else:
        base = Path(args.workspace).expanduser() / "marketing" / "xiaohongshu"
        date_str = datetime.now().strftime("%Y%m%d")
        out_path = base / f"{date_str}-{slugify(args.topic)}.md"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

