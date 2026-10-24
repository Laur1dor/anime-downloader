"""Trusted release controller. Model proposals never execute on the host."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from maintenance.policy import validate, source_hash

ROOT = Path(os.environ.get("REPAIR_PROJECT_ROOT", "/root/anime-bot"))
STATE = Path(os.environ.get("REPAIR_STATE_ROOT", "/root/anime-repair"))
COMPOSE = ["docker", "compose", "-f", "compose.yml", "-f", "compose.tunnel.yml",
           "-f", "compose.library.yml", "-f", "compose.observability.yml"]
if os.environ.get("REPAIR_COMPOSE_FILES"):
    COMPOSE = ["docker", "compose"]
    for filename in json.loads(os.environ["REPAIR_COMPOSE_FILES"]):
        COMPOSE += ["-f", filename]
CONTAINER = os.environ.get("REPAIR_CONTAINER", "anime-bot")


def run(args, *, timeout=120, check=True, **kwargs):
    result = subprocess.run(args, cwd=ROOT, timeout=timeout, capture_output=True,
                            text=True, **kwargs)
    if check and result.returncode:
        raise RuntimeError(f"Command failed: {args[0]} ({result.returncode})")
    return result


def snapshot(root=ROOT):
    names = ["bot.py", "requirements.txt"]
    for folder in ("animedl", "tests"):
        names += [p.relative_to(root).as_posix() for p in (root / folder).rglob("*")
                  if p.is_file() and p.suffix in {".py", ".html"}
                  and "__pycache__" not in p.parts and p.name != "test_public_export.py"]
    result = {}
    for name in names:
        p = root / name
        if p.is_symlink() or any(x.is_symlink() for x in p.parents if x != root):
            raise ValueError("Linked source")
        result[name] = p.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    return result


def atomic(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temp.open('w',encoding='utf-8') as output:
        json.dump(value,output,ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    temp.replace(path)


def journal():
    p = STATE / "transaction.json"
    return json.loads(p.read_text()) if p.exists() else None


def image():
    return run(["docker", "inspect", CONTAINER, "--format", "{{.Image}}"], timeout=15).stdout.strip()


def health(after=0, *, draining=None):
    try:
        h = json.loads((ROOT / "data/runtime-health.json").read_text())
        return (h if h["ready"] and time.time()-h["ts"] < 10 and h["started"] >= after
                and (draining is None or h["draining"] == draining) else None)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def replace_image(tag):
    override = STATE / "image.json"
    atomic(override, {"services": {"bot": {"image": tag}}})
    run([*COMPOSE, "-f", str(override), "up", "-d", "--no-deps", "--no-build", "--force-recreate", "bot"], timeout=120)


def sandbox(image_id, source, command, timeout=300):
    """No credentials, sockets, production data, network or writable source mount."""
    name = "animedl-check-" + uuid.uuid4().hex[:12]
    args = ["docker", "run", "--rm", "--name", name, "--network", "none",
            "--user", "65534:65534", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "768m", "--cpus", "0.75", "--pids-limit", "128", "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=384m", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "HOME=/tmp", "-v", f"{source}:/app:ro", "-w", "/app", image_id, *command]
    try:
        return run(args, timeout=timeout, check=False)
    finally:
        run(["docker", "rm", "-f", name], check=False, timeout=15)


def prune_candidates():
    import re
    root = STATE / 'candidates'
    if not root.exists():
        return
    tx = journal() or {}
    protected = {image(), tx.get('previous')}
    entries = sorted((p for p in root.iterdir() if re.fullmatch(r'[a-f0-9]{32}', p.name)
                      and p.is_dir() and not p.is_symlink()), key=lambda p:p.stat().st_mtime, reverse=True)
    for path in entries[10:]:
        if path.name == tx.get('id') or path.resolve().parent != root.resolve():
            continue
        tag = 'anime-bot-repair:' + path.name
        found = run(['docker','image','inspect',tag,'--format','{{.Id}}'], check=False, timeout=15)
        if found.stdout.strip() in protected:
            continue
        run(['docker','image','rm',tag,'anime-bot-repair-base:'+path.name],check=False,timeout=30)
        shutil.rmtree(path)


def prepare(request):
    prune_candidates()
    if shutil.disk_usage(ROOT).free < 512 * 1024 * 1024:
        raise ValueError("Insufficient free disk for isolated repair")
    original = snapshot()
    if source_hash(original) != request["source"]:
        raise ValueError("Source changed; proposal is stale")
    changes = validate(request["proposal"], original)
    request_id = uuid.uuid4().hex
    target = STATE / "candidates" / request_id
    target.mkdir(parents=True)
    base, candidate = target / "base", target / "candidate"
    for root, files in ((base, original), (candidate, original | changes)):
        for name, content in files.items():
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        # Tools are trusted and cannot be changed by the proposal.
        shutil.copytree(Path(__file__).parent, root / "maintenance", ignore=shutil.ignore_patterns("__pycache__"))
        root.chmod(0o755)
    test = request["proposal"]["test"]["path"]
    (base / test).write_text(changes[test], encoding="utf-8")
    current_image = image()
    command = ["python", "-m", "unittest", "discover", "-s", "tests", "-p", Path(test).name, "-v"]
    old = sandbox(current_image, base, command)
    new = sandbox(current_image, candidate, command)
    # The only failing baseline test must be the newly added reproduction.
    # Test the unmodified suite separately without that test.
    (base / test).unlink()
    clean = sandbox(current_image, base, ["python", "-m", "unittest", "discover", "-s", "tests"], 600)
    full = sandbox(current_image, candidate, ["python", "-m", "unittest", "discover", "-s", "tests"], 600)
    for name, result in (("regression-before", old), ("regression-after", new), ("baseline", clean), ("suite", full)):
        (target / (name + ".log")).write_text((result.stdout + result.stderr)[-150000:], encoding="utf-8")
    if old.returncode == 0 or new.returncode or clean.returncode or full.returncode or "skipped=" in new.stderr or "Ran 0 tests" in new.stderr:
        raise ValueError("Reproduction or isolated suite rejected repair")
    if "FAIL:" not in old.stderr and "ERROR:" not in old.stderr:
        raise ValueError("Baseline did not demonstrate a test assertion failure")
    # Reuse installed dependencies; never run a generated Dockerfile or dependency install.
    base_tag = "anime-bot-repair-base:" + request_id
    run(["docker", "image", "tag", current_image, base_tag], timeout=15)
    recipe = "FROM " + base_tag + "\nCOPY animedl/ /app/animedl/\nCOPY bot.py /app/bot.py\n"
    (candidate / "Dockerfile.repair").write_text(recipe)
    tag = "anime-bot-repair:" + request_id
    built = run(["docker", "build", "--network", "none", "-f", str(candidate / "Dockerfile.repair"), "-t", tag, str(candidate)], timeout=300, check=False)
    (target / "build.log").write_text((built.stdout + built.stderr)[-150000:], encoding="utf-8")
    if built.returncode:
        raise ValueError("Candidate image build failed")
    report = dict(id=request_id, source=request["source"], image=tag, previous=current_image,
                  changes=changes, summary=request["proposal"]["summary"][:1000], created=time.time())
    atomic(target / "prepared.json", report)
    return {"id": request_id, "status": "tested", "source": request["source"]}


def rollback(tx):
    replace_image(tx["previous"])
    for name, content in tx["originals"].items():
        p = ROOT / name
        if content is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(content, encoding="utf-8")
    (ROOT / "data/release.drain").unlink(missing_ok=True)
    tx["status"] = "rolled_back"
    atomic(STATE / "transaction.json", tx)


def watchdog():
    tx = journal()
    if tx and tx["status"] == "activating" and time.time() > tx["deadline"]:
        rollback(tx)
        return {"status": "rolled_back", "id": tx["id"]}
    drain = ROOT / "data/release.drain"
    if (not tx or tx["status"] != "activating") and drain.exists() and time.time()-drain.stat().st_mtime > 240:
        drain.unlink()
    return {"status": tx["status"] if tx else "idle", "id": tx["id"] if tx else None}


def abort(request_id):
    tx = journal()
    if tx and tx["id"] == request_id and tx["status"] in {"activating", "committed"}:
        rollback(tx)
        return {"status": "rolled_back", "id": request_id}
    return {"status": "not_active", "id": request_id}


def activate(request_id, commit):
    if not __import__('re').fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("Invalid candidate id")
    if not __import__('re').fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Verified public commit required")
    prepared = json.loads((STATE / "candidates" / request_id / "prepared.json").read_text())
    if time.time()-prepared["created"] > 3600 or source_hash(snapshot()) != prepared["source"] or image() != prepared["previous"]:
        raise ValueError("Stale candidate")
    if not health():
        raise ValueError("Current process has no fresh health heartbeat")
    drain = ROOT / "data/release.drain"
    drain.touch()
    try:
        end = time.time()+180
        quiet = 0
        while time.time() < end:
            h = health(draining=True)
            quiet = quiet + 1 if h and h["active"] == 0 else 0
            if quiet >= 2:
                break
            time.sleep(3)
        else:
            raise ValueError("Busy; active downloads were not interrupted")
        originals = {name: ((ROOT / name).read_text(encoding="utf-8") if (ROOT / name).exists() else None)
                     for name in prepared["changes"]}
        tx = prepared | dict(status="activating", deadline=time.time()+180,
                             originals=originals, public_commit=commit)
        atomic(STATE / "transaction.json", tx)  # watchdog can recover even if this process dies
        try:
            started = time.time()
            replace_image(prepared["image"])
            stable_until = time.time()+60
            while time.time() < stable_until:
                time.sleep(3)
                h = health(after=started, draining=True)
                inspected = json.loads(run(["docker", "inspect", CONTAINER], timeout=10).stdout)[0]
                if inspected["RestartCount"] or inspected["State"]["Status"] != "running":
                    raise ValueError("Candidate process failed")
                if time.time()-started > 20 and not h:
                    raise ValueError("Candidate heartbeat failed")
            for name, content in prepared["changes"].items():
                (ROOT / name).write_text(content, encoding="utf-8")
            if journal()["status"] != "activating" or time.time() > tx["deadline"]:
                raise ValueError("Watchdog expired the release")
            tx["status"] = "committed"
            atomic(STATE / "transaction.json", tx)
            atomic(ROOT / ".repair-release.json", dict(id=request_id, public_commit=commit, image=prepared["image"], ts=time.time()))
            return {"status": "committed", "id": request_id}
        except BaseException:
            rollback(tx)
            raise
    finally:
        drain.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["snapshot", "prepare", "activate", "abort", "watchdog"])
    parser.add_argument("--request")
    parser.add_argument("--commit")
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    if args.action == "watchdog":
        print(json.dumps(watchdog()))
        return
    with (STATE / "release.lock").open("w") as lock:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "snapshot":
            result = snapshot()
        elif args.action == "prepare":
            result = prepare(json.loads(Path(args.request).read_text()))
        elif args.action == "activate":
            result = activate(args.request, args.commit)
        elif args.action == "abort":
            result = abort(args.request)
        else:
            result = watchdog()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
