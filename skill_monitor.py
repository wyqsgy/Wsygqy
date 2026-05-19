# -*- coding: utf-8 -*-
"""
全局技能同步状态监控器 - 并发检查远程服务器状态 + 技能同步状态
当执行编程任务时, 并发检查以下内容:
  1. 远程服务器连接状态 (SSH)
  2. 守护进程运行状态
  3. 技能同步是否与 GitHub 网页端保持每24h实时更新
  4. 上次同步时间与最新 commit 时间对比

运行方式:
  python skill_monitor.py                        (本地并发检查)
  python skill_monitor.py --json                 (JSON 格式输出)
  python skill_monitor.py --remote-only          (仅检查远程)
  python skill_monitor.py --local-only           (仅检查本地)
"""

import os
import sys
import json
import time
import argparse
import logging
import hashlib
import subprocess
import urllib.request
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

# ============================================================
# 配置区 - 非敏感信息
# ============================================================

REMOTE_HOST = "123.56.200.34"
REMOTE_PORT = 22
REMOTE_USER = "root"
REMOTE_PASSWORD = "1357924680wyqSGY."

REMOTE_PROJECT_DIR = "/root/codex-agent"
GITHUB_REPO_URL = "https://github.com/anbeime/skill"
GITHUB_API_URL = "https://api.github.com/repos/anbeime/skill"
GITHUB_COMMITS_URL = f"{GITHUB_API_URL}/commits"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
LOCAL_STATUS_FILE = os.path.join(SCRIPT_DIR, ".sync_status.json")
LOCAL_SKILLS_DIR = os.path.join(PROJECT_ROOT, ".trae", "skills")

CHECK_TIMEOUT = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("SkillMonitor")


def fetch_github_latest_commit() -> dict:
    """从 GitHub API 获取仓库最新 commit 信息"""
    result = {
        "success": False,
        "last_commit_time": None,
        "last_commit_sha": None,
        "last_commit_message": "",
        "error": None,
    }

    try:
        req = urllib.request.Request(
            GITHUB_COMMITS_URL + "?per_page=1",
            headers={"Accept": "application/vnd.github.v3+json",
                     "User-Agent": "SkillMonitor/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            if data and isinstance(data, list) and len(data) > 0:
                commit = data[0]
                result["success"] = True
                result["last_commit_sha"] = commit.get("sha", "")[:8]
                result["last_commit_message"] = commit.get("commit", {}).get("message", "").split("\n")[0]
                result["last_commit_time"] = commit.get("commit", {}).get("committer", {}).get("date")
    except Exception as e:
        result["error"] = str(e)

    return result


def count_local_skills() -> int:
    """统计本地技能数量"""
    count = 0
    if os.path.isdir(LOCAL_SKILLS_DIR):
        for item in os.listdir(LOCAL_SKILLS_DIR):
            item_path = os.path.join(LOCAL_SKILLS_DIR, item)
            if os.path.isdir(item_path):
                skill_md = os.path.join(item_path, "SKILL.md")
                if os.path.isfile(skill_md):
                    count += 1
    return count


def read_local_status() -> Optional[dict]:
    """读取本地同步状态文件"""
    if os.path.exists(LOCAL_STATUS_FILE):
        try:
            with open(LOCAL_STATUS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def check_remote_server() -> dict:
    """通过 SSH 检查远程服务器状态"""
    result = {
        "target": "remote_server",
        "host": REMOTE_HOST,
        "connected": False,
        "daemon_running": False,
        "crontab_set": False,
        "last_sync_time": None,
        "skill_count": 0,
        "uptime": None,
        "error": None,
    }

    try:
        import paramiko
    except ImportError:
        result["error"] = "paramiko 未安装"
        return result

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=REMOTE_HOST,
            port=REMOTE_PORT,
            username=REMOTE_USER,
            password=REMOTE_PASSWORD,
            timeout=CHECK_TIMEOUT,
        )
        result["connected"] = True

        _, stdout, _ = client.exec_command("uptime", timeout=10)
        result["uptime"] = stdout.read().decode("utf-8").strip()

        _, stdout, _ = client.exec_command(
            f"cat {REMOTE_PROJECT_DIR}/.sync_status.json 2>/dev/null || echo 'NO_STATUS'",
            timeout=10,
        )
        status_raw = stdout.read().decode("utf-8").strip()
        if status_raw and status_raw != "NO_STATUS":
            try:
                status_data = json.loads(status_raw)
                result["last_sync_time"] = status_data.get("last_sync")
                result["skill_count"] = status_data.get("skill_count", 0)
            except json.JSONDecodeError:
                pass

        _, stdout, _ = client.exec_command(
            "ps aux | grep 'skill_sync.py.*daemon' | grep -v grep | wc -l",
            timeout=10,
        )
        daemon_count = stdout.read().decode("utf-8").strip()
        result["daemon_running"] = int(daemon_count) > 0 if daemon_count.isdigit() else False

        _, stdout, _ = client.exec_command(
            "crontab -l 2>/dev/null | grep 'skill_sync' | wc -l",
            timeout=10,
        )
        cron_count = stdout.read().decode("utf-8").strip()
        result["crontab_set"] = int(cron_count) > 0 if cron_count.isdigit() else False

        client.close()
    except Exception as e:
        result["error"] = str(e)

    return result


def check_sync_health() -> dict:
    """检查技能同步健康度 (综合本地 + GitHub 远程对比)"""
    result = {
        "target": "sync_health",
        "is_syncing": False,
        "local_skill_count": 0,
        "remote_skill_count": 0,
        "github_last_commit": None,
        "local_last_sync": None,
        "sync_delay_hours": None,
        "status": "UNKNOWN",
        "recommendation": "",
    }

    github_info = fetch_github_latest_commit()
    result["github_last_commit"] = github_info.get("last_commit_time")

    local_status = read_local_status()
    if local_status:
        result["local_last_sync"] = local_status.get("last_sync")
        result["local_skill_count"] = local_status.get("skill_count", 0)
    else:
        result["local_skill_count"] = count_local_skills()

    if result["local_last_sync"] and result["github_last_commit"]:
        try:
            sync_dt = datetime.fromisoformat(result["local_last_sync"])
            github_dt = datetime.fromisoformat(result["github_last_commit"].replace("Z", "+00:00"))
            github_dt = github_dt.replace(tzinfo=None)
            result["sync_delay_hours"] = round(abs((github_dt - sync_dt).total_seconds()) / 3600, 1)

            if result["sync_delay_hours"] <= 24:
                result["status"] = "OK"
                result["recommendation"] = "技能同步正常, 与上游仓库更新时间差在 24 小时内"
            elif result["sync_delay_hours"] <= 48:
                result["status"] = "WARNING"
                result["recommendation"] = "技能同步可能有延迟, 建议手动触发一次同步"
            else:
                result["status"] = "STALE"
                result["recommendation"] = "技能同步严重滞后! 请检查守护进程和 crontab 状态"
        except Exception:
            result["status"] = "ERROR"
            result["recommendation"] = "时间解析失败, 请检查同步状态文件"

    return result


def run_concurrent_checks() -> dict:
    """并发执行所有检查"""
    results = {}

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(check_remote_server): "remote_server",
            executor.submit(check_sync_health): "sync_health",
            executor.submit(fetch_github_latest_commit): "github_info",
        }

        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result(timeout=30)
            except Exception as e:
                results[key] = {"error": str(e), "target": key}

    return results


def format_report(results: dict) -> str:
    """格式化输出检查报告"""
    lines = []
    lines.append("=" * 65)
    lines.append("  技能同步系统 - 全局状态检查报告")
    lines.append(f"  检查时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 65)

    remote = results.get("remote_server", {})
    lines.append(f"\n  >>> 远程服务器 [{REMOTE_HOST}]")
    if remote.get("connected"):
        lines.append(f"    状态: 在线")
        lines.append(f"    运行时间: {remote.get('uptime', 'N/A')}")
        lines.append(f"    守护进程: {'运行中' if remote.get('daemon_running') else '未运行'}")
        lines.append(f"    Crontab:   {'已配置' if remote.get('crontab_set') else '未配置'}")
        lines.append(f"    最后同步: {remote.get('last_sync_time', '无记录')}")
        lines.append(f"    技能数量: {remote.get('skill_count', 0)}")
    else:
        lines.append(f"    状态: 离线")
        lines.append(f"    错误: {remote.get('error', '未知')}")

    sync = results.get("sync_health", {})
    lines.append(f"\n  >>> 同步健康度")
    lines.append(f"    状态:     {sync.get('status', 'N/A')}")
    lines.append(f"    本地技能: {sync.get('local_skill_count', 0)}")
    lines.append(f"    最后同步: {sync.get('local_last_sync', '无')}")
    lines.append(f"    GitHub最新: {sync.get('github_last_commit', 'N/A')}")
    if sync.get("sync_delay_hours") is not None:
        lines.append(f"    同步延迟: {sync['sync_delay_hours']} 小时")
    if sync.get("recommendation"):
        lines.append(f"    建议:     {sync['recommendation']}")

    github = results.get("github_info", {})
    if github.get("success"):
        lines.append(f"\n  >>> GitHub 仓库 [{GITHUB_REPO_URL}]")
        lines.append(f"    最新提交: {github.get('last_commit_sha', 'N/A')}")
        lines.append(f"    提交信息: {github.get('last_commit_message', 'N/A')}")
        lines.append(f"    提交时间: {github.get('last_commit_time', 'N/A')}")
    elif github.get("error"):
        lines.append(f"\n  >>> GitHub API: 访问失败 ({github['error']})")

    lines.append("\n" + "=" * 65)

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="全局技能同步状态监控器")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--remote-only", action="store_true", help="仅检查远程服务器")
    parser.add_argument("--local-only", action="store_true", help="仅检查本地同步状态")

    args = parser.parse_args()

    if args.remote_only:
        results = {"remote_server": check_remote_server()}
    elif args.local_only:
        results = {"sync_health": check_sync_health()}
    else:
        logger.info("开始并发检查 (最多 4 路并发)...")
        results = run_concurrent_checks()

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(format_report(results))


if __name__ == "__main__":
    main()