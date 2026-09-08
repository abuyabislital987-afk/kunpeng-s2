#!/usr/bin/env python3
"""一次交互登录，后续工具复用 SSH 连接；不保存密码。"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from cluster import ROOT, ClusterError, load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config' / 'cluster.local.json')
    args = parser.parse_args()
    cfg = load_config(args.config)
    socket = Path(cfg.get('control_path') or str(ROOT / '.runs' / 'ssh-control')).expanduser()
    if not socket.is_absolute():
        raise ClusterError('control_path 必须是绝对路径')
    socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket.parent.chmod(0o700)
    if len(str(socket).encode()) > 95:
        raise ClusterError('control_path 太长；请选一个更短的本机私有目录')
    if not sys.stdin.isatty():
        raise ClusterError('请在可交互终端运行此脚本，直接向 SSH 输入密码')
    if not cfg.get('control_path'):
        raw = json.loads(args.config.read_text())
        raw['control_path'] = str(socket)
        args.config.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + '\n')
        args.config.chmod(0o600)
    command = ['ssh', '-M', '-S', str(socket), '-o', 'ControlPersist=3600',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10', '-p', str(cfg['port'])]
    if cfg.get('known_hosts'):
        command += ['-o', 'UserKnownHostsFile=' + cfg['known_hosts']]
    destination = (cfg['user'] + '@' if cfg.get('user') else '') + cfg['host']
    command.append(destination)
    print('请在下面直接输入超算密码（不会显示或保存）。登录后若调度器要求认证，请执行 dlogin。', flush=True)
    print('保持这个终端连接，告诉 Agent“登录好了”，后续上传/测评会复用会话。', flush=True)
    return subprocess.call(command)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ClusterError, OSError) as exc:
        print('错误: ' + str(exc), file=sys.stderr)
        sys.exit(2)
