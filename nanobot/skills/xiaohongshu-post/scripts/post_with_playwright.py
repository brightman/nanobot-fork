#!/usr/bin/env python3
"""Post a Xiaohongshu markdown draft via Playwright browser automation."""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish"
LOGIN_URL = "https://creator.xiaohongshu.com/"


@dataclass
class Draft:
    title: str
    body: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post Xiaohongshu draft with Playwright.")
    parser.add_argument("--post-file", required=True, help="Markdown draft path.")
    parser.add_argument(
        "--state-file",
        default="~/.nanobot/workspace/.auth/xiaohongshu_state.json",
        help="Playwright storage state file.",
    )
    parser.add_argument("--login", action="store_true", help="Login and save session state only.")
    parser.add_argument("--publish", action="store_true", help="Click publish button.")
    parser.add_argument("--headless", action="store_true", help="Run browser headless.")
    parser.add_argument("--debug", action="store_true", help="Print debug logs and keep page open longer.")
    parser.add_argument("--timeout-ms", type=int, default=20000, help="Action timeout in ms.")
    return parser.parse_args()


def parse_markdown(path: Path) -> Draft:
    text = path.read_text(encoding="utf-8")
    title = ""
    body = ""
    current = None
    body_lines: list[str] = []

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("# 标题"):
            current = "title"
            continue
        if line.startswith("# 正文"):
            current = "body"
            continue
        if line.startswith("# ") and current == "body":
            current = None
            continue

        if current == "title" and line and not title:
            title = line
        elif current == "body":
            body_lines.append(raw.rstrip())

    if not title:
        for raw in text.splitlines():
            if raw.strip():
                title = raw.strip().lstrip("#").strip()
                break

    if not body_lines:
        body = text.strip()
    else:
        body = "\n".join([ln for ln in body_lines]).strip()

    if not title:
        raise ValueError("Cannot parse title from markdown file.")
    if not body:
        raise ValueError("Cannot parse body from markdown file.")
    return Draft(title=title[:20], body=body)


def wait_for_manual_login(page: Page, state_file: Path, timeout_seconds: int = 180) -> None:
    print(f"[login] Opened {LOGIN_URL}")
    print("[login] Please scan QR / complete login in browser window.")
    print("[login] Waiting for creator domain session...")

    # Open once and let user complete interactive login without forced refresh.
    page.goto(LOGIN_URL, wait_until="domcontentloaded")

    start = time.time()
    while time.time() - start < timeout_seconds:
        try:
            # If login flow redirects to publish page, this will become true naturally.
            if is_publish_editor_page(page):
                state_file.parent.mkdir(parents=True, exist_ok=True)
                page.context.storage_state(path=str(state_file))
                print(f"[ok] Login state saved to: {state_file}")
                return

            # If user lands on creator home after login, nudge once to publish page.
            if "creator.xiaohongshu.com" in page.url and not is_login_page(page):
                page.goto(PUBLISH_URL, wait_until="domcontentloaded")
                if is_publish_editor_page(page):
                    state_file.parent.mkdir(parents=True, exist_ok=True)
                    page.context.storage_state(path=str(state_file))
                    print(f"[ok] Login state saved to: {state_file}")
                    return
        except Exception:
            # Ignore transient navigation states while user interacts with auth widgets.
            pass

        # Poll only; do not hard-refresh to avoid interrupting QR/SMS login.
        time.sleep(2)

    raise TimeoutError("Login timeout. Could not reach publish page within expected time.")


def fill_first_visible(page: Page, selectors: list[str], value: str, timeout_ms: int) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() == 0:
                continue
            target = locator.first
            target.wait_for(state="visible", timeout=timeout_ms)
            target.click()
            target.fill("")
            target.type(value, delay=20)
            return True
        except Exception:
            continue
    return False


def fill_contenteditable(page: Page, selectors: list[str], value: str, timeout_ms: int) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() == 0:
                continue
            target = locator.first
            target.wait_for(state="visible", timeout=timeout_ms)
            target.click()
            page.keyboard.press("Meta+A")
            page.keyboard.type(value, delay=8)
            return True
        except Exception:
            continue
    return False


def click_publish(page: Page, timeout_ms: int) -> bool:
    candidates = ["发布", "立即发布", "发布笔记", "发布内容"]
    for name in candidates:
        try:
            btn = page.get_by_role("button", name=name).first
            btn.wait_for(state="visible", timeout=1500)
            btn.click(timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


def is_login_page(page: Page) -> bool:
    url = page.url.lower()
    if "/login" in url:
        return True
    if page.locator("input[placeholder*='手机号']").count() > 0:
        return True
    if page.locator("input[placeholder*='验证码']").count() > 0:
        return True
    return False


def is_publish_editor_page(page: Page) -> bool:
    if is_login_page(page):
        return False

    url = page.url.lower()
    if "/publish/publish" in url:
        return True

    # Prefer positive signals from publish editor UI.
    signals = [
        "input[placeholder*='标题']",
        "textarea[placeholder*='标题']",
        "div[contenteditable='true']",
        ".ql-editor[contenteditable='true']",
        "button:has-text('发布')",
        "button:has-text('立即发布')",
    ]
    for selector in signals:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    return False


def post_draft(page: Page, draft: Draft, timeout_ms: int, do_publish: bool, debug: bool) -> None:
    page.goto(PUBLISH_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    if is_login_page(page):
        raise RuntimeError("Not logged in: redirected to Xiaohongshu login page.")

    title_ok = fill_first_visible(
        page,
        selectors=[
            "input[placeholder*='标题']",
            "textarea[placeholder*='标题']",
            "input[placeholder*='请填写标题']",
            "textarea[placeholder*='请填写标题']",
            "input[maxlength='20']",
            "textarea[maxlength='20']",
        ],
        value=draft.title,
        timeout_ms=timeout_ms,
    )
    if not title_ok:
        raise RuntimeError("Failed to locate title input.")

    content_ok = fill_contenteditable(
        page,
        selectors=[
            "div[contenteditable='true']",
            ".ql-editor[contenteditable='true']",
            ".editor[contenteditable='true']",
            "[data-placeholder*='正文'][contenteditable='true']",
            "[placeholder*='正文']",
        ],
        value=draft.body,
        timeout_ms=timeout_ms,
    )
    if not content_ok:
        raise RuntimeError("Failed to locate content editor.")

    print("[ok] Draft filled into Xiaohongshu editor.")
    if do_publish:
        ok = click_publish(page, timeout_ms=timeout_ms)
        if not ok:
            raise RuntimeError("Failed to locate publish button.")
        print("[ok] Publish button clicked. Please verify final confirmation in UI.")
    else:
        print("[dry-run] Filled only. Not clicked publish.")

    if debug:
        page.wait_for_timeout(5000)


def main() -> int:
    args = parse_args()
    post_file = Path(args.post_file).expanduser()
    if not post_file.exists():
        print(f"[error] post file not found: {post_file}", file=sys.stderr)
        return 1

    state_file = Path(args.state_file).expanduser()
    draft = parse_markdown(post_file)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        context_kwargs = {}
        if state_file.exists():
            context_kwargs["storage_state"] = str(state_file)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.set_default_timeout(args.timeout_ms)

        try:
            if args.login or not state_file.exists():
                wait_for_manual_login(page, state_file=state_file)
                if args.login and not args.publish:
                    print("[done] Login finished.")
                    return 0

            post_draft(
                page=page,
                draft=draft,
                timeout_ms=args.timeout_ms,
                do_publish=args.publish,
                debug=args.debug,
            )
            context.storage_state(path=str(state_file))
            return 0
        except (PlaywrightTimeoutError, TimeoutError, RuntimeError, ValueError) as exc:
            screenshot = post_file.parent / "xiaohongshu_publish_error.png"
            try:
                page.screenshot(path=str(screenshot), full_page=True)
                print(f"[debug] screenshot: {screenshot}")
            except Exception:
                pass
            print(f"[error] {exc}", file=sys.stderr)
            return 2
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
