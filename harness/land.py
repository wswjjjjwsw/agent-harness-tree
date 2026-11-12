"""文档落盘 doc/<node_id>.md（76 §3.4，task-def §3.7）。

★ 落盘**只在驱动器侧**发生：模型出内容（doc 全文 / edits 增量），驱动器落盘 ——
  模型不拿文件系统权限。整个 harness/ 里**只有本文件**写 doc/（可自检：
  `grep -rn "doc/" harness/ | grep -v land.py` 应只剩注释/常量）。
★ 原子写：临时文件 + os.replace（半截文件永远不会被别的读者看到）。
★ 空文本 ⇒ raise（不许落一个空文档），且**不落盘**。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from harness import role as role_mod

DEFAULT_DOC_DIR = role_mod.DEFAULT_DOC_DIR   # 缺省 <树根>/doc，可用 TREE_DOC_DIR 覆盖


def _safe_node_id(node_id: str) -> str:
    """node_id 是文件名，不许跳出 doc/（防 ../ 之类把文件写到 doc/ 外面）。"""
    nid = str(node_id or "").strip()
    if not nid:
        raise ValueError("node_id 不能为空（它是文件名）")
    if "/" in nid or "\\" in nid or nid in (".", ".."):
        raise ValueError("node_id 里不许含路径分隔符或 . / ..：%r" % node_id)
    return nid


def _atomic_text_write(target: Path, text: str) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix="." + target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(text))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return str(target.resolve())


def land_doc(doc_dir: str, node_id: str, text: str) -> str:
    """写 <doc_dir>/<node_id>.md；原子写；父目录自建。"""
    if text is None or not str(text).strip():
        raise ValueError("doc 文本为空/空白 ⇒ 拒绝落盘（不许落一个空文档）")
    nid = _safe_node_id(node_id)
    target = Path(doc_dir or DEFAULT_DOC_DIR) / (nid + ".md")
    return _atomic_text_write(target, text)


def land_run_file(run_dir: str, relative_path: str, text: str) -> str:
    """Atomically write one explicitly bound text file under a run directory."""
    rel = Path(str(relative_path or ""))
    if (not str(relative_path or "").strip() or rel.is_absolute()
            or ".." in rel.parts or rel.suffix.lower() not in (".md", ".txt")):
        raise ValueError("managed document target must be a run-relative .md/.txt path")
    if text is None or not str(text).strip():
        raise ValueError("managed document text is empty")
    root = Path(run_dir).resolve()
    target = (root / rel).resolve()
    if os.path.commonpath((str(root), str(target))) != str(root):
        raise ValueError("managed document target escapes run directory")
    return _atomic_text_write(target, text)
