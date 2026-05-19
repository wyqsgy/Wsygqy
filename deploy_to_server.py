# -*- coding: utf-8 -*-
"""
远程服务器部署脚本 - 将 skill_sync.py 部署到 123.56.200.34
功能:
  1. SSH 连接到远程服务器
  2. 上传同步脚本及相关文件
  3. 安装 Python 依赖 (paramiko 等)
  4. 配置 crontab 定时任务 (每12小时)
  5. 启动守护进程
  6. 验证部署状态

运行方式:
  python deploy_to_server.py           (完整部署)
  python deploy_to_server.py --check   (仅检查远程状态)
  python deploy_to_server.py --restart (重启远程守护进程)
"""

import os
import sys
import json
import time
import argparse
import logging
import subprocess
from pathlib import Path
from datetime import datetime

# ============================================================
# 配置区 - 非敏感信息
# ============================================================

REMOTE_HOST = "123.56.200.34"
REMOTE_PORT = 22
REMOTE_USER = "root"
REMOTE_PASSWORD = "1357924680wyqSGY."

REMOTE_PROJECT_DIR = "/root/codex-agent"
REMOTE_SYNC_SCRIPT = "skill_sync.py"
LOCAL_SYNC_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skill_sync.py")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("Deploy")


def rpath(*parts: str) -> str:
    """构建远程 Linux 路径 (始终使用 / 分隔符)"""
    result = "/".join(p.strip("/") for p in parts if p)
    if parts and parts[0].startswith("/"):
        result = "/" + result
    return result or "/"


def check_paramiko() -> bool:
    """检查 paramiko 是否安装"""
    try:
        import paramiko
        return True
    except ImportError:
        return False


def install_paramiko():
    """安装 paramiko"""
    logger.info("安装 paramiko...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "paramiko", "-q"])


def get_ssh_client():
    """创建 SSH 客户端并连接"""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=REMOTE_HOST,
        port=REMOTE_PORT,
        username=REMOTE_USER,
        password=REMOTE_PASSWORD,
        timeout=30,
    )
    return client


def remote_exec(client, command: str, timeout: int = 60) -> tuple:
    """在远程服务器上执行命令, 返回 (exit_code, stdout, stderr)"""
    logger.info(f"  [EXEC] {command[:120]}")
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return exit_code, out, err


def remote_exec_nohup(client, command: str) -> tuple:
    """执行 nohup 后台命令 (不等待输出)"""
    logger.info(f"  [EXEC-NOHUP] {command[:120]}")
    transport = client.get_transport()
    channel = transport.open_session()
    channel.exec_command(command)
    time.sleep(2)
    exit_code = channel.recv_exit_status() if not channel.closed else -1
    out = ""
    err = ""
    try:
        out = channel.recv(4096).decode("utf-8", errors="replace")
    except Exception:
        pass
    try:
        err = channel.recv_stderr(4096).decode("utf-8", errors="replace")
    except Exception:
        pass
    channel.close()
    return exit_code, out, err


def upload_file(client, local_path: str, remote_path: str):
    """通过 SFTP 上传文件到远程服务器"""
    import paramiko
    sftp = client.open_sftp()
    try:
        remote_dir = os.path.dirname(remote_path)
        try:
            sftp.stat(remote_dir)
        except FileNotFoundError:
            remote_exec(client, f"mkdir -p {remote_dir}")

        sftp.put(local_path, remote_path)
        logger.info(f"  上传成功: {local_path} -> {remote_path}")
    finally:
        sftp.close()


def setup_remote_environment(client) -> bool:
    """配置远程服务器环境"""
    logger.info("\n" + "=" * 60)
    logger.info("Step 1: 配置远程服务器环境")
    logger.info("=" * 60)

    commands = [
        "which python3 || which python",
        "python3 --version 2>&1 || python --version 2>&1",
        "which git",
        "which pip3 || which pip",
    ]

    for cmd in commands:
        code, out, err = remote_exec(client, cmd)
        logger.info(f"  {cmd}: {out.strip() or err.strip()}")

    logger.info("创建项目目录...")
    remote_exec(client, f"mkdir -p {REMOTE_PROJECT_DIR}")
    remote_exec(client, f"mkdir -p {REMOTE_PROJECT_DIR}/logs")

    logger.info("安装 Python 依赖...")
    pip_cmd = "pip3" if "python3" in str(remote_exec(client, "which python3")[1]) else "pip"
    remote_exec(client, f"{pip_cmd} install --upgrade pip -q 2>&1 || true", timeout=120)
    remote_exec(client, f"{pip_cmd} install paramiko requests -q 2>&1 || true", timeout=120)

    return True


def deploy_sync_script(client) -> bool:
    """上传同步脚本"""
    logger.info("\n" + "=" * 60)
    logger.info("Step 2: 上传同步脚本")
    logger.info("=" * 60)

    remote_path = rpath(REMOTE_PROJECT_DIR, REMOTE_SYNC_SCRIPT)
    upload_file(client, LOCAL_SYNC_SCRIPT, remote_path)

    code, out, err = remote_exec(client, f"ls -la {remote_path}")
    logger.info(f"  远程文件: {out.strip()}")

    return True


def configure_crontab(client) -> bool:
    """配置 crontab 定时任务 (每12小时)"""
    logger.info("\n" + "=" * 60)
    logger.info("Step 3: 配置 Crontab 定时任务")
    logger.info("=" * 60)

    script_path = rpath(REMOTE_PROJECT_DIR, REMOTE_SYNC_SCRIPT)
    log_path = rpath(REMOTE_PROJECT_DIR, "logs", "cron_sync.log")

    cron_job = f"0 */12 * * * cd {REMOTE_PROJECT_DIR} && python3 {script_path} --once >> {log_path} 2>&1"

    remove_old_cmd = f"(crontab -l 2>/dev/null | grep -v 'skill_sync.py') | crontab - 2>/dev/null || true"
    remote_exec(client, remove_old_cmd)

    add_cmd = f'(crontab -l 2>/dev/null; echo "{cron_job}") | crontab -'
    code, out, err = remote_exec(client, add_cmd)

    code, out, err = remote_exec(client, "crontab -l")
    logger.info(f"  当前 crontab:\n{out}")

    return True


def start_remote_daemon(client) -> bool:
    """启动远程守护进程"""
    logger.info("\n" + "=" * 60)
    logger.info("Step 4: 启动远程守护进程")
    logger.info("=" * 60)

    remote_exec(client, "pkill -f 'skill_sync.py.*daemon' 2>/dev/null || true")
    time.sleep(2)

    script_path = rpath(REMOTE_PROJECT_DIR, REMOTE_SYNC_SCRIPT)
    log_path = rpath(REMOTE_PROJECT_DIR, "logs", "daemon.log")

    daemon_cmd = (
        f"cd {REMOTE_PROJECT_DIR} && "
        f"nohup python3 {script_path} --daemon > {log_path} 2>&1 &"
    )
    code, out, err = remote_exec_nohup(client, daemon_cmd)
    time.sleep(3)

    code, out, err = remote_exec(client, "ps aux | grep 'skill_sync.py' | grep -v grep")
    if out.strip():
        logger.info(f"  守护进程已启动:\n{out}")
        return True
    else:
        logger.warning("  守护进程可能未成功启动, 请检查日志")
        code, out, err = remote_exec(client, f"tail -20 {log_path}")
        logger.info(f"  最近日志:\n{out}")
        return False


def verify_deployment(client) -> dict:
    """验证部署状态"""
    logger.info("\n" + "=" * 60)
    logger.info("Step 5: 验证部署状态")
    logger.info("=" * 60)

    result = {
        "host": REMOTE_HOST,
        "deployed": False,
        "daemon_running": False,
        "crontab_set": False,
        "git_available": False,
        "python_available": False,
    }

    code, out, err = remote_exec(client, "which python3 || which python")
    result["python_available"] = bool(out.strip())

    code, out, err = remote_exec(client, "which git")
    result["git_available"] = bool(out.strip())

    script_path = rpath(REMOTE_PROJECT_DIR, REMOTE_SYNC_SCRIPT)
    code, out, err = remote_exec(client, f"test -f {script_path} && echo 'EXISTS'")
    result["deployed"] = "EXISTS" in out

    code, out, err = remote_exec(client, "ps aux | grep 'skill_sync.py.*daemon' | grep -v grep | wc -l")
    result["daemon_running"] = int(out.strip()) > 0 if out.strip().isdigit() else False

    code, out, err = remote_exec(client, "crontab -l 2>/dev/null | grep 'skill_sync' | wc -l")
    result["crontab_set"] = int(out.strip()) > 0 if out.strip().isdigit() else False

    status_path = rpath(REMOTE_PROJECT_DIR, ".sync_status.json")
    code, out, err = remote_exec(client, f"cat {status_path} 2>/dev/null || echo 'NO_STATUS'")
    if out.strip() and out.strip() != "NO_STATUS":
        try:
            status_data = json.loads(out.strip())
            result["last_sync"] = status_data.get("last_sync")
            result["skill_count"] = status_data.get("skill_count", 0)
        except json.JSONDecodeError:
            pass

    for key, val in result.items():
        if isinstance(val, bool):
            status = "OK" if val else "FAIL"
            logger.info(f"  {key}: {status}")
        else:
            logger.info(f"  {key}: {val}")

    return result


def check_remote_status():
    """仅检查远程服务器状态"""
    if not check_paramiko():
        install_paramiko()

    logger.info(f"检查远程服务器: {REMOTE_HOST}")
    client = get_ssh_client()
    try:
        logger.info("连接成功!")

        code, out, err = remote_exec(
            client,
            f"cat {REMOTE_PROJECT_DIR}/.sync_status.json 2>/dev/null || echo 'NO_STATUS'"
        )
        if out.strip() and out.strip() != "NO_STATUS":
            status = json.loads(out.strip())
            logger.info(f"\n同步状态:")
            logger.info(f"  最后同步: {status.get('last_sync', 'N/A')}")
            logger.info(f"  技能数量: {status.get('skill_count', 0)}")
            logger.info(f"  同步成功: {status.get('success', False)}")

        code, out, err = remote_exec(
            client,
            "ps aux | grep 'skill_sync.py' | grep -v grep"
        )
        if out.strip():
            logger.info(f"\n运行进程:\n{out}")
        else:
            logger.info("\n无运行中的同步进程")

        code, out, err = remote_exec(client, "crontab -l 2>/dev/null | grep skill_sync")
        if out.strip():
            logger.info(f"\nCrontab 定时任务:\n{out}")
        else:
            logger.info("\n无 crontab 定时任务")
    finally:
        client.close()


def restart_remote_daemon():
    """重启远程守护进程"""
    if not check_paramiko():
        install_paramiko()

    logger.info(f"连接远程服务器: {REMOTE_HOST}")
    client = get_ssh_client()
    try:
        start_remote_daemon(client)
        verify_deployment(client)
    finally:
        client.close()


def full_deploy():
    """完整部署流程"""
    logger.info("=" * 60)
    logger.info("  技能同步系统 - 远程服务器部署")
    logger.info(f"  目标服务器: {REMOTE_HOST}:{REMOTE_PORT}")
    logger.info(f"  部署时间: {datetime.now()}")
    logger.info("=" * 60)

    if not check_paramiko():
        install_paramiko()

    if not os.path.exists(LOCAL_SYNC_SCRIPT):
        logger.error(f"同步脚本不存在: {LOCAL_SYNC_SCRIPT}")
        sys.exit(1)

    logger.info(f"连接远程服务器 {REMOTE_HOST}...")
    try:
        client = get_ssh_client()
        logger.info("SSH 连接成功!")
    except Exception as e:
        logger.error(f"SSH 连接失败: {e}")
        sys.exit(1)

    try:
        setup_remote_environment(client)
        deploy_sync_script(client)
        configure_crontab(client)
        start_remote_daemon(client)
        result = verify_deployment(client)

        logger.info("\n" + "=" * 60)
        if result["deployed"] and result["daemon_running"]:
            logger.info("  部署成功!")
        else:
            logger.warning("  部署可能存在问题, 请检查上述输出")
        logger.info("=" * 60)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description="技能同步系统 - 远程部署工具")
    parser.add_argument("--deploy", action="store_true", help="完整部署到远程服务器 (默认)")
    parser.add_argument("--check", action="store_true", help="仅检查远程服务器状态")
    parser.add_argument("--restart", action="store_true", help="重启远程守护进程")

    args = parser.parse_args()

    if args.check:
        check_remote_status()
    elif args.restart:
        restart_remote_daemon()
    else:
        full_deploy()


if __name__ == "__main__":
    main()