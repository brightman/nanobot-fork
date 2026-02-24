"""External supervisor for safe online upgrades."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    clean = "".join(ch.lower() if ch.isalnum() else "-" for ch in text)
    clean = "-".join(part for part in clean.split("-") if part)
    return clean[:40] or "upgrade"


@dataclass
class UpgradeRequest:
    """A queued request for supervised upgrade."""

    id: str
    title: str
    prompt: str
    scope: str = "core"  # skill | core
    requested_by: str = "unknown"
    created_at: str = field(default_factory=_utc_now)

    @classmethod
    def create(cls, title: str, prompt: str, scope: str = "core", requested_by: str = "unknown") -> "UpgradeRequest":
        return cls(
            id=f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
            title=title.strip(),
            prompt=prompt.strip(),
            scope=scope,
            requested_by=requested_by,
        )


class UpgradeTaskStore:
    """Persistent task queue + markdown task log."""

    def __init__(self, workspace: Path):
        self.root = workspace / "upgrades"
        self.queue = self.root / "queue"
        self.tasks = self.root / "tasks"
        self.logs = self.root / "logs"
        self.backups = self.root / "backups"
        self.worktrees = self.root / "worktrees"
        for d in (self.queue, self.tasks, self.logs, self.backups, self.worktrees):
            d.mkdir(parents=True, exist_ok=True)

    def enqueue(self, req: UpgradeRequest) -> Path:
        req_path = self.queue / f"{req.id}.json"
        req_path.write_text(json.dumps(asdict(req), ensure_ascii=False, indent=2), encoding="utf-8")
        self.write_task_md(req, status="QUEUED", notes=["Task queued."])
        return req_path

    def list_pending(self) -> list[Path]:
        return sorted(self.queue.glob("*.json"), key=lambda p: p.name)

    def load(self, path: Path) -> UpgradeRequest:
        data = json.loads(path.read_text(encoding="utf-8"))
        return UpgradeRequest(**data)

    def pop(self, path: Path) -> None:
        if path.exists():
            path.unlink()

    def write_task_md(self, req: UpgradeRequest, status: str, notes: list[str] | None = None) -> None:
        notes = notes or []
        content = [
            f"# Upgrade Task {req.id}",
            "",
            f"- Title: {req.title}",
            f"- Scope: {req.scope}",
            f"- Requested By: {req.requested_by}",
            f"- Created At (UTC): {req.created_at}",
            f"- Last Updated (UTC): {_utc_now()}",
            f"- Status: {status}",
            "",
            "## Prompt",
            "",
            req.prompt,
            "",
            "## Notes",
        ]
        for n in notes:
            content.append(f"- {n}")

        (self.tasks / f"{req.id}.md").write_text("\n".join(content) + "\n", encoding="utf-8")


class UpgradeSupervisor:
    """Runs queued upgrade requests in isolated worktrees and deploys safely."""

    def __init__(
        self,
        workspace: Path,
        repo_root: Path,
        service_cmd: str = "python3 -m nanobot gateway",
        build_timeout_s: int = 30 * 60,
        health_grace_s: int = 20,
    ):
        self.workspace = workspace
        self.repo_root = repo_root
        self.service_cmd = service_cmd
        self.build_timeout_s = build_timeout_s
        self.health_grace_s = health_grace_s
        self.store = UpgradeTaskStore(workspace)
        self.service_proc: subprocess.Popen[str] | None = None
        self.service_log = self.store.logs / "service.log"

    def submit(self, req: UpgradeRequest) -> Path:
        return self.store.enqueue(req)

    def run_forever(self, poll_interval_s: int = 3) -> None:
        logger.info("Supervisor started. workspace={} repo={}", self.workspace, self.repo_root)
        self._ensure_service_running()

        while True:
            self._ensure_service_running()

            pending = self.store.list_pending()
            if pending:
                req_file = pending[0]
                req = self.store.load(req_file)
                try:
                    self._process(req)
                except Exception as e:
                    logger.exception("Upgrade task {} crashed", req.id)
                    self.store.write_task_md(req, status="FAILED", notes=[f"Supervisor exception: {e}"])
                finally:
                    self.store.pop(req_file)

            time.sleep(poll_interval_s)

    def _process(self, req: UpgradeRequest) -> None:
        if req.scope != "core":
            self.store.write_task_md(
                req,
                status="REJECTED",
                notes=["Non-core task: should be handled by skill installation/update pipeline."],
            )
            return

        clean, detail = self._repo_is_clean()
        if not clean:
            self.store.write_task_md(req, status="FAILED", notes=[f"Repo not clean: {detail}"])
            return

        self.store.write_task_md(req, status="IN_PROGRESS", notes=["Creating isolated worktree."])

        branch = f"codex/upgrade-{_slug(req.title)}-{req.id[-6:]}"
        wt_dir = self.store.worktrees / req.id

        self._run_cmd(
            ["git", "worktree", "add", "-B", branch, str(wt_dir), "HEAD"],
            cwd=self.repo_root,
            timeout=120,
        )

        try:
            ok, notes = self._develop_and_verify(req, wt_dir)
            if not ok:
                self.store.write_task_md(req, status="FAILED", notes=notes)
                return

            deploy_ok, deploy_notes = self._deploy(req, wt_dir)
            self.store.write_task_md(
                req,
                status="SUCCESS" if deploy_ok else "FAILED",
                notes=notes + deploy_notes,
            )
        finally:
            self._run_cmd(["git", "worktree", "remove", "--force", str(wt_dir)], cwd=self.repo_root, timeout=120)

    def _develop_and_verify(self, req: UpgradeRequest, wt_dir: Path) -> tuple[bool, list[str]]:
        deadline = time.time() + self.build_timeout_s
        notes: list[str] = []
        feedback = ""
        attempt = 0

        while time.time() < deadline:
            attempt += 1
            notes.append(f"Attempt {attempt}: coding in sandbox.")

            prompt = (
                "You are upgrading nanobot core. "
                "Rules: make minimal safe changes, keep service stable, and run quick self-checks. "
                "Task:\n"
                f"{req.prompt}\n\n"
                "If previous attempt failed, fix based on this feedback:\n"
                f"{feedback or '(none)'}"
            )

            codex_ok, codex_out = self._run_optional_codex(prompt, wt_dir, timeout=min(900, max(60, int(deadline - time.time()))))
            notes.append("Codex exec finished." if codex_ok else f"Codex exec issue: {codex_out[:300]}")

            verify_ok, verify_notes = self._verify(wt_dir)
            notes.extend(verify_notes)
            if verify_ok:
                notes.append("Verification passed.")
                return True, notes

            feedback = "\n".join(verify_notes[-8:])

        notes.append("Timeout exceeded (30 minutes) before successful verification.")
        return False, notes

    def _verify(self, cwd: Path) -> tuple[bool, list[str]]:
        notes: list[str] = []

        checks: list[tuple[str, list[str]]] = [
            ("compile", ["python3", "-m", "compileall", "nanobot"]),
            ("ruff", ["ruff", "check", "."]),
            ("pytest", ["pytest", "-q"]),
        ]

        executed = 0
        for name, cmd in checks:
            if shutil.which(cmd[0]) is None:
                notes.append(f"Skipped {name}: '{cmd[0]}' not found.")
                continue
            executed += 1
            ok, out = self._run_cmd(cmd, cwd=cwd, timeout=600, check=False)
            if not ok:
                notes.append(f"{name} failed.")
                notes.append(self._trim(out, 1200))
                return False, notes
            notes.append(f"{name} passed.")

        if executed == 0:
            notes.append("No verification tools available.")
            return False, notes

        return True, notes

    def _deploy(self, req: UpgradeRequest, wt_dir: Path) -> tuple[bool, list[str]]:
        notes: list[str] = []

        backup_patch = self.store.backups / f"{req.id}-forward.patch"
        rollback_patch = self.store.backups / f"{req.id}-rollback.patch"

        ok, diff = self._run_cmd(["git", "diff", "--binary", "HEAD"], cwd=wt_dir, timeout=120, check=False)
        if not ok:
            return False, ["Failed to export candidate patch.", self._trim(diff, 600)]
        if not diff.strip():
            return False, ["No code changes were produced."]

        backup_patch.write_text(diff, encoding="utf-8")
        notes.append(f"Saved candidate patch: {backup_patch}")

        ok, out = self._run_cmd(["git", "apply", "--index", str(backup_patch)], cwd=self.repo_root, timeout=120, check=False)
        if not ok:
            return False, ["Failed to apply candidate patch to live repo.", self._trim(out, 800)]

        ok, reverse = self._run_cmd(["git", "diff", "--binary", "--cached"], cwd=self.repo_root, timeout=120, check=False)
        if ok and reverse.strip():
            rollback_patch.write_text(reverse, encoding="utf-8")
            notes.append(f"Saved rollback patch: {rollback_patch}")

        # Cutover: restart service with upgraded code.
        old_proc = self.service_proc
        self._stop_service()
        started = self._start_service()
        if started:
            notes.append("New service started successfully; cutover complete.")
            return True, notes

        # Roll back on startup failure.
        notes.append("New service failed health check; starting rollback.")
        self._run_cmd(["git", "apply", "-R", "--index", str(backup_patch)], cwd=self.repo_root, timeout=120, check=False)
        self._stop_service()
        rollback_started = self._start_service()
        if rollback_started:
            notes.append("Rollback succeeded; previous service restored.")
        else:
            notes.append("Rollback failed to restore service automatically; manual intervention needed.")

        if old_proc and old_proc.poll() is None:
            # Ensure no orphan process when fallback path took too long.
            self._terminate_process(old_proc)

        return False, notes

    def _run_optional_codex(self, prompt: str, cwd: Path, timeout: int) -> tuple[bool, str]:
        if shutil.which("codex") is None:
            return False, "codex CLI not installed"

        cmd = [
            "codex",
            "exec",
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            prompt,
        ]
        return self._run_cmd(cmd, cwd=cwd, timeout=timeout, check=False)

    def _repo_is_clean(self) -> tuple[bool, str]:
        ok, out = self._run_cmd(["git", "status", "--porcelain"], cwd=self.repo_root, timeout=30, check=False)
        if not ok:
            return False, "failed to read git status"
        dirty = out.strip()
        if dirty:
            return False, dirty[:500]
        return True, "clean"

    def _ensure_service_running(self) -> None:
        if self.service_proc is None or self.service_proc.poll() is not None:
            self._start_service()

    def _start_service(self) -> bool:
        self.service_log.parent.mkdir(parents=True, exist_ok=True)
        logf = self.service_log.open("a", encoding="utf-8")
        self.service_proc = subprocess.Popen(
            self.service_cmd,
            cwd=self.repo_root,
            shell=True,
            stdout=logf,
            stderr=logf,
            text=True,
            start_new_session=True,
        )
        time.sleep(self.health_grace_s)
        healthy = self.service_proc.poll() is None
        if not healthy:
            logger.error("Service failed to stay alive. cmd={}", self.service_cmd)
        return healthy

    def _stop_service(self) -> None:
        if not self.service_proc:
            return
        self._terminate_process(self.service_proc)
        self.service_proc = None

    def _terminate_process(self, proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, 15)
            proc.wait(timeout=20)
        except Exception:
            try:
                os.killpg(proc.pid, 9)
            except Exception:
                pass

    @staticmethod
    def _run_cmd(
        cmd: list[str],
        cwd: Path,
        timeout: int,
        check: bool = True,
    ) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                timeout=timeout,
                capture_output=True,
                text=True,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, f"Timeout after {timeout}s: {' '.join(cmd)}"
        except Exception as e:
            return False, f"Command failed to start ({' '.join(cmd)}): {e}"

        output = (proc.stdout or "") + ("\nSTDERR:\n" + proc.stderr if proc.stderr else "")
        if check and proc.returncode != 0:
            return False, output or f"Exit code {proc.returncode}"
        return proc.returncode == 0, output

    @staticmethod
    def _trim(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n... (truncated {len(text) - limit} chars)"

    def status(self) -> dict[str, Any]:
        pending = self.store.list_pending()
        return {
            "service_running": self.service_proc is not None and self.service_proc.poll() is None,
            "pending_tasks": len(pending),
            "latest_pending": pending[0].name if pending else None,
            "service_log": str(self.service_log),
            "tasks_dir": str(self.store.tasks),
        }
