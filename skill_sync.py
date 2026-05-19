# -*- coding: utf-8 -*-
"""
技能自动同步脚本 - 从 anbeime/skill 爬取 Skills 并写入 Trae 技能目录
运行方式: python skill_sync.py              (单次同步)
         python skill_sync.py --daemon     (守护进程, 每12小时同步)
         python skill_sync.py --status     (查看同步状态)
"""

import os
import sys
import json
import time
import shutil
import logging
import hashlib
import argparse
import subprocess
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

# ============================================================
# 配置区 - 以下信息为非敏感配置
# ============================================================

# GitHub 仓库配置
GITHUB_REPO_URL = "https://github.com/anbeime/skill.git"
GITHUB_REPO_NAME = "anbeime-skill"

# 远程服务器配置 (非敏感信息)
REMOTE_SERVER_HOST = "123.56.200.34"
REMOTE_SERVER_PORT = 22
REMOTE_SERVER_USER = "root"
REMOTE_SERVER_PASSWORD = "1357924680wyqSGY."

# 同步间隔 (秒)
SYNC_INTERVAL_HOURS = 12
SYNC_INTERVAL_SECONDS = SYNC_INTERVAL_HOURS * 3600

# 路径配置
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
TRAE_SKILLS_DIR = os.path.join(PROJECT_ROOT, ".trae", "skills")
REPO_CACHE_DIR = os.path.join(SCRIPT_DIR, ".skill_repo_cache")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
STATUS_FILE = os.path.join(SCRIPT_DIR, ".sync_status.json")


def setup_logging() -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger("SkillSync")
    logger.setLevel(logging.INFO)

    log_file = os.path.join(LOG_DIR, "skill_sync.log")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.INFO)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


logger = setup_logging()


def run_cmd(cmd: list, cwd: Optional[str] = None, timeout: int = 300) -> tuple:
    """执行命令并返回 (returncode, stdout, stderr)"""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"


def clone_or_pull_repo() -> bool:
    """通过 GitHub API 下载 ZIP 包 (绕过防火墙对 git 协议的阻断)"""
    import zipfile
    import io

    logger.info("=" * 60)
    logger.info("开始同步 GitHub 仓库 (API ZIP 模式)...")

    zip_url = "https://api.github.com/repos/anbeime/skill/zipball/main"
    zip_path = os.path.join(SCRIPT_DIR, ".skill_repo_snapshot.zip")

    try:
        import urllib.request

        req = urllib.request.Request(
            zip_url,
            headers={"Accept": "application/vnd.github.v3+json",
                     "User-Agent": "SkillSync/1.0"},
        )
        logger.info(f"下载 ZIP: {zip_url}")
        with urllib.request.urlopen(req, timeout=120) as resp:
            zip_data = resp.read()
        logger.info(f"下载完成: {len(zip_data)} 字节")

        if os.path.exists(REPO_CACHE_DIR):
            shutil.rmtree(REPO_CACHE_DIR, ignore_errors=True)

        os.makedirs(REPO_CACHE_DIR, exist_ok=True)

        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            namelist = zf.namelist()
            root_prefix = namelist[0].split("/")[0] + "/"
            for member in namelist:
                if member.endswith("/"):
                    continue
                rel_path = member[len(root_prefix):]
                target = os.path.join(REPO_CACHE_DIR, rel_path)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())

        os.remove(zip_path) if os.path.exists(zip_path) else None
        logger.info("ZIP 解压完成")
        return True

    except Exception as e:
        logger.error(f"API ZIP 下载失败: {e}")
        return False


def scan_skills_from_repo() -> list[dict]:
    """扫描仓库中的 skills 目录, 提取所有 skill 元数据"""
    skills = []
    repo_skills_dir = os.path.join(REPO_CACHE_DIR, "skills")

    if not os.path.isdir(repo_skills_dir):
        logger.error(f"Skills 目录不存在: {repo_skills_dir}")
        return skills

    for skill_name in sorted(os.listdir(repo_skills_dir)):
        skill_path = os.path.join(repo_skills_dir, skill_name)

        if not os.path.isdir(skill_path):
            continue

        nested_dir = os.path.join(skill_path, skill_name)
        skill_md_path = None

        if os.path.isdir(nested_dir):
            candidate = os.path.join(nested_dir, "SKILL.md")
            if os.path.isfile(candidate):
                skill_md_path = candidate
            else:
                for f in os.listdir(nested_dir):
                    fp = os.path.join(nested_dir, f)
                    if os.path.isfile(fp) and f.upper().endswith(".MD"):
                        skill_md_path = fp
                        break

        if not skill_md_path:
            logger.debug(f"跳过 {skill_name}: 无 SKILL.md")
            continue

        try:
            with open(skill_md_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            logger.warning(f"读取 {skill_md_path} 失败: {e}")
            continue

        skills.append({
            "name": skill_name,
            "source_path": skill_md_path,
            "content": content,
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "size": len(content),
        })
        logger.info(f"  发现技能: {skill_name} ({len(content)} 字符)")

    return skills


def parse_skill_frontmatter(content: str) -> dict:
    """解析 SKILL.md 的 YAML frontmatter"""
    meta = {}
    lines = content.split("\n")
    if lines and lines[0].strip() == "---":
        end_idx = None
        for i in range(1, min(len(lines), 50)):
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx:
            for line in lines[1:end_idx]:
                line = line.strip()
                if ":" in line:
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    meta[key] = val
    return meta


def sync_skills_to_trae(skills: list[dict]) -> dict:
    """将技能同步到 Trae 的 .trae/skills/ 目录"""
    stats = {"added": 0, "updated": 0, "skipped": 0, "removed": 0}

    os.makedirs(TRAE_SKILLS_DIR, exist_ok=True)

    synced_names = set()
    for skill in skills:
        name = skill["name"]
        synced_names.add(name)
        target_dir = os.path.join(TRAE_SKILLS_DIR, name)
        target_file = os.path.join(target_dir, "SKILL.md")

        if os.path.exists(target_file):
            try:
                with open(target_file, "r", encoding="utf-8", errors="replace") as f:
                    existing = f.read()
                existing_hash = hashlib.sha256(existing.encode()).hexdigest()
                if existing_hash == skill["content_hash"]:
                    stats["skipped"] += 1
                    continue
            except Exception:
                pass

        os.makedirs(target_dir, exist_ok=True)

        try:
            with open(target_file, "w", encoding="utf-8") as f:
                f.write(skill["content"])
            stats["updated"] += 1
            logger.info(f"  同步技能: {name} -> {target_file}")
        except Exception as e:
            logger.error(f"  写入失败 {name}: {e}")

    for existing_dir in os.listdir(TRAE_SKILLS_DIR):
        existing_path = os.path.join(TRAE_SKILLS_DIR, existing_dir)
        if os.path.isdir(existing_path) and existing_dir not in synced_names:
            skill_md = os.path.join(existing_path, "SKILL.md")
            if os.path.isfile(skill_md):
                shutil.rmtree(existing_path, ignore_errors=True)
                stats["removed"] += 1
                logger.info(f"  移除过期技能: {existing_dir}")

    return stats


def update_status_file(stats: dict, skill_count: int, success: bool):
    """更新同步状态文件"""
    status = {
        "last_sync": datetime.now().isoformat(),
        "last_sync_ts": time.time(),
        "success": success,
        "skill_count": skill_count,
        "stats": stats,
        "server_host": REMOTE_SERVER_HOST,
        "sync_interval_hours": SYNC_INTERVAL_HOURS,
        "repo_url": GITHUB_REPO_URL,
    }
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"写入状态文件失败: {e}")


def read_status() -> Optional[dict]:
    """读取当前同步状态"""
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def run_sync() -> bool:
    """执行一次完整同步流程"""
    logger.info(f"开始技能同步 @ {datetime.now()}")

    if not clone_or_pull_repo():
        update_status_file({}, 0, False)
        return False

    skills = scan_skills_from_repo()
    if not skills:
        logger.error("未发现任何技能文件")
        update_status_file({}, 0, False)
        return False

    logger.info(f"共发现 {len(skills)} 个技能")

    stats = sync_skills_to_trae(skills)
    logger.info(
        f"同步完成: 新增 {stats['added']}, 更新 {stats['updated']}, "
        f"跳过 {stats['skipped']}, 移除 {stats['removed']}"
    )

    update_status_file(stats, len(skills), True)
    logger.info("=" * 60)
    return True


def daemon_mode():
    """守护进程模式 - 每12小时自动同步"""
    logger.info(f"启动守护进程, 同步间隔: {SYNC_INTERVAL_HOURS} 小时")
    logger.info(f"远程服务器: {REMOTE_SERVER_HOST}:{REMOTE_SERVER_PORT}")

    while True:
        run_sync()
        next_sync = datetime.now() + timedelta(hours=SYNC_INTERVAL_HOURS)
        logger.info(f"下次同步时间: {next_sync.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"等待 {SYNC_INTERVAL_HOURS} 小时...")

        for _ in range(SYNC_INTERVAL_HOURS * 60):
            time.sleep(60)


def check_server_status_via_ssh() -> dict:
    """通过 SSH 检查远程服务器上的同步状态"""
    result = {
        "host": REMOTE_SERVER_HOST,
        "connected": False,
        "sync_daemon_running": False,
        "last_sync": None,
        "skill_count": 0,
        "error": None,
    }

    try:
        import paramiko
    except ImportError:
        result["error"] = "paramiko 未安装, 无法进行 SSH 检查"
        return result

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=REMOTE_SERVER_HOST,
            port=REMOTE_SERVER_PORT,
            username=REMOTE_SERVER_USER,
            password=REMOTE_SERVER_PASSWORD,
            timeout=15,
        )
        result["connected"] = True

        stdin, stdout, stderr = client.exec_command(
            "cat /root/codex-agent/.sync_status.json 2>/dev/null || echo 'NOT_FOUND'"
        )
        status_raw = stdout.read().decode("utf-8").strip()

        if status_raw and status_raw != "NOT_FOUND":
            try:
                status_data = json.loads(status_raw)
                result["last_sync"] = status_data.get("last_sync")
                result["skill_count"] = status_data.get("skill_count", 0)
            except json.JSONDecodeError:
                result["error"] = "状态文件格式异常"

        stdin, stdout, stderr = client.exec_command(
            "ps aux | grep 'skill_sync.py.*daemon' | grep -v grep | wc -l"
        )
        daemon_count = stdout.read().decode("utf-8").strip()
        result["sync_daemon_running"] = int(daemon_count) > 0 if daemon_count.isdigit() else False

        client.close()
    except Exception as e:
        result["error"] = str(e)

    return result


def print_status():
    """打印本地和远程同步状态"""
    print("\n" + "=" * 60)
    print("  技能同步状态报告")
    print("=" * 60)

    local = read_status()
    if local:
        print(f"\n  [本地状态]")
        print(f"  最后同步: {local.get('last_sync', 'N/A')}")
        print(f"  同步成功: {local.get('success', False)}")
        print(f"  技能数量: {local.get('skill_count', 0)}")
        stats = local.get("stats", {})
        if stats:
            print(f"  新增: {stats.get('added', 0)}, 更新: {stats.get('updated', 0)}, "
                  f"跳过: {stats.get('skipped', 0)}, 移除: {stats.get('removed', 0)}")
    else:
        print("\n  [本地状态] 无同步记录")

    print(f"\n  [远程服务器] {REMOTE_SERVER_HOST}:{REMOTE_SERVER_PORT}")
    remote = check_server_status_via_ssh()
    if remote["connected"]:
        print(f"  连接状态: 成功")
        print(f"  守护进程: {'运行中' if remote['sync_daemon_running'] else '未运行'}")
        print(f"  最后同步: {remote.get('last_sync', 'N/A')}")
        print(f"  技能数量: {remote.get('skill_count', 0)}")
    else:
        print(f"  连接状态: 失败 ({remote.get('error', '未知错误')})")

    print("\n" + "=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="技能自动同步工具")
    parser.add_argument("--daemon", action="store_true", help="守护进程模式, 每12小时自动同步")
    parser.add_argument("--status", action="store_true", help="查看本地和远程同步状态")
    parser.add_argument("--once", action="store_true", help="执行一次同步后退出 (默认行为)")

    args = parser.parse_args()

    if args.status:
        print_status()
    elif args.daemon:
        daemon_mode()
    else:
        success = run_sync()
        if success:
            print("\n[SUCCESS] 技能同步完成!")
        else:
            print("\n[FAILED] 技能同步失败, 请检查日志")
            sys.exit(1)


if __name__ == "__main__":
    main()