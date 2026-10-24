"""Fail-closed patch policy and durable repair budgets."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path, PurePosixPath

ALLOWED = {"animedl/sites/yummyanime.py", "animedl/sites/animego.py",
           "animedl/sites/sopranos.py", "animedl/players/alloha.py",
           "animedl/players/kodik.py", "animedl/players/aniboom.py",
           "animedl/players/cvh.py", "animedl/players/plplayer.py"}
CLASSES = {"ValueError", "KeyError", "IndexError", "TypeError", "AttributeError",
           "JSONDecodeError", "ExtractorError", "ExtractError", "ParseError", "RuntimeError"}


def eligible(details):
    return (details.get("error_class") in CLASSES
            and details.get("http_status") is None
            and any(details.get("location", "").startswith(Path(p).name + ":") for p in ALLOWED)
            and re.fullmatch(r"[a-f0-9]{16}", details.get("fingerprint", "")) is not None)


def validate(proposal, original):
    """No deletes, test rewrites, dependencies, infrastructure or path escapes."""
    if not isinstance(proposal, dict) or set(proposal) != {"summary", "files", "test"}:
        raise ValueError("Invalid proposal structure")
    files, test = proposal["files"], proposal["test"]
    if not isinstance(files, list) or not 1 <= len(files) <= 2:
        raise ValueError("Repair must change one or two parser files")
    result = {}
    for item in [*files, test]:
        expected = {"path", "content"} if item is test else {"path", "old", "new"}
        if not isinstance(item, dict) or set(item) != expected:
            raise ValueError("Invalid file structure")
        name = item["path"]
        if not isinstance(name, str):
            raise ValueError("Text files required")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or str(path) != name:
            raise ValueError("Unsafe path")
        if name in result:
            raise ValueError("Duplicate file")
        if item is test:
            if not re.fullmatch(r"tests/test_repair_[a-z0-9_]+\.py", name) or name in original:
                raise ValueError("A new independent regression test is required")
            content = item["content"]
        elif name not in ALLOWED or name not in original:
            raise ValueError("File outside automatic repair scope")
        else:
            old, new = item["old"], item["new"]
            if not isinstance(old, str) or not isinstance(new, str) or not old or len(old)+len(new) > 20000:
                raise ValueError("Small exact replacements required")
            if original[name].count(old) != 1:
                raise ValueError("Replacement must match exactly once")
            content = original[name].replace(old, new, 1)
        if not isinstance(content, str):
            raise ValueError("Text files required")
        if not content.strip() or len(content.encode()) > 120000:
            raise ValueError("Empty or oversized file")
        tree = ast.parse(content, filename=name)
        if item is not test:
            before_tree = ast.parse(original[name])
            imports = lambda t: sorted(ast.dump(n) for n in ast.walk(t) if isinstance(n, (ast.Import, ast.ImportFrom)))
            if imports(tree) != imports(before_tree):
                raise ValueError("New imports require manual review")
        # Execution occurs in an unprivileged, offline container as a second boundary.
        forbidden = {"subprocess", "socket", "ctypes", "paramiko", "shutil"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(n.name.split('.')[0] in forbidden for n in node.names):
                raise ValueError("Process or host access is outside repair scope")
            if isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[0] in forbidden:
                raise ValueError("Process or host access is outside repair scope")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
                    "eval", "exec", "__import__", "compile", "open", "getattr", "setattr", "globals", "locals"}:
                raise ValueError("Dynamic execution is outside repair scope")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {
                    "system", "popen", "spawn", "execv", "execve", "write_text", "write_bytes", "unlink", "rmdir", "remove", "rename"}:
                raise ValueError("Host mutation is outside repair scope")
        result[name] = content
    if all(original[x["path"]] == result[x["path"]] for x in files):
        raise ValueError("No runtime change")
    # Bound the actual edit, not the existing module size.
    import difflib
    changed = sum(sum(1 for line in difflib.ndiff(original[x['path']].splitlines(), result[x['path']].splitlines())
                      if line[:2] in ('+ ', '- ')) for x in files)
    if changed > 120:
        raise ValueError("Repair exceeds 120 changed lines")
    return result


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS attempts (fingerprint TEXT, source TEXT, ts REAL, status TEXT, report TEXT)")
        self.db.commit()

    def close(self):
        self.db.close()

    def claim(self, fingerprint, source, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM attempts WHERE fingerprint=? AND source=?", (fingerprint, source)).fetchone():
                return False
            if self.db.execute("SELECT COUNT(*) FROM attempts WHERE ts>?", (now-86400,)).fetchone()[0] >= 3:
                return False
            self.db.execute("INSERT INTO attempts VALUES (?,?,?,'claimed','')", (fingerprint, source, now))
        return True

    def finish(self, fingerprint, source, status, report):
        with self.db:
            self.db.execute("UPDATE attempts SET status=?,report=? WHERE fingerprint=? AND source=?",
                            (status, json.dumps(report, ensure_ascii=False), fingerprint, source))


def source_hash(files):
    digest = hashlib.sha256()
    for name, value in sorted(files.items()):
        digest.update(name.encode() + b"\0" + value.replace("\r\n", "\n").encode() + b"\0")
    return digest.hexdigest()
