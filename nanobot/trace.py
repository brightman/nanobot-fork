"""Lightweight runtime tracing for nanobot."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from loguru import logger


def _now_iso() -> str:
    return datetime.now().isoformat()


def _safe_json(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except Exception:
        return str(value)


@dataclass
class _Span:
    span_id: str
    parent_span_id: str | None
    name: str
    start_ts: float
    attrs: dict[str, Any]


class TraceRecorder:
    """Append-only JSONL trace writer with best-effort durability."""

    def __init__(
        self,
        workspace: Path,
        *,
        trace_id: str | None = None,
        run_id: str | None = None,
        channel: str = "unknown",
        chat_id: str = "unknown",
        sender_id: str = "unknown",
        session_key: str = "unknown",
    ) -> None:
        self.workspace = workspace
        self.trace_id = trace_id or uuid.uuid4().hex[:16]
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.channel = channel
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.session_key = session_key
        self._span_stack: list[_Span] = []
        self._run_start = time.monotonic()
        self._turn = 0
        date_dir = datetime.now().strftime("%Y-%m-%d")
        self.path = self.workspace / "traces" / date_dir / f"{self.trace_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write(
            {
                "kind": "run_start",
                "status": "running",
                "attrs": {},
            }
        )

    @property
    def current_turn(self) -> int:
        return self._turn

    def set_turn(self, turn: int) -> None:
        self._turn = turn

    def _base(self) -> dict[str, Any]:
        return {
            "ts": _now_iso(),
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "channel": self.channel,
            "chat_id": self.chat_id,
            "sender_id": self.sender_id,
            "session_key": self.session_key,
            "turn": self._turn,
        }

    def _write(self, record: dict[str, Any]) -> None:
        data = {**self._base(), **record}
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(_safe_json(data), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("Trace write failed: {}", e)

    def event(self, name: str, attrs: dict[str, Any] | None = None) -> None:
        self._write(
            {
                "kind": "event",
                "name": name,
                "attrs": _safe_json(attrs or {}),
            }
        )

    @contextmanager
    def span(self, name: str, attrs: dict[str, Any] | None = None) -> Iterator[None]:
        parent_id = self._span_stack[-1].span_id if self._span_stack else None
        span = _Span(
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=parent_id,
            name=name,
            start_ts=time.monotonic(),
            attrs=attrs or {},
        )
        self._span_stack.append(span)
        self._write(
            {
                "kind": "span_start",
                "name": name,
                "span_id": span.span_id,
                "parent_span_id": parent_id,
                "attrs": _safe_json(span.attrs),
            }
        )
        status = "ok"
        error: str | None = None
        try:
            yield
        except Exception as e:
            status = "error"
            error = str(e)
            raise
        finally:
            duration_ms = int((time.monotonic() - span.start_ts) * 1000)
            self._write(
                {
                    "kind": "span_end",
                    "name": name,
                    "span_id": span.span_id,
                    "parent_span_id": parent_id,
                    "status": status,
                    "duration_ms": duration_ms,
                    "error": error,
                }
            )
            if self._span_stack and self._span_stack[-1].span_id == span.span_id:
                self._span_stack.pop()

    def finish(self, *, status: str, final_contract: str, error: str | None = None) -> None:
        elapsed_ms = int((time.monotonic() - self._run_start) * 1000)
        self._write(
            {
                "kind": "run_end",
                "status": status,
                "final_contract": final_contract,
                "elapsed_ms": elapsed_ms,
                "error": error,
            }
        )


def traces_root(workspace: Path) -> Path:
    return workspace / "traces"


def iter_trace_files(workspace: Path) -> list[Path]:
    root = traces_root(workspace)
    if not root.exists():
        return []
    files = [p for p in root.rglob("*.jsonl") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def read_trace(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return rows
    return rows

