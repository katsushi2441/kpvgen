"""H3 を投入する前に、0.14 の GPU が空いているかを確かめる。

**なぜ要るか**: 2026-09-18、0.14 に常駐させた判定サービス（jevlocal）が 9GB の VRAM を
握ったまま H3 を投入し、2本とも「out of memory」で落とした。rqdb4ai は Ollama と H3 を
直列化するが、**キューの外で動いている常駐プロセスは直列化できない**。
落ちるのは20分後なので、投入前に見ておくほうが早い。

使い方（enqueue_h3.py の先頭で）:

    from h3_preflight import check_or_die
    check_or_die()            # 足りなければ、何が握っているかを出して止まる
    check_or_die(warn=True)   # 止めずに警告だけ
"""
from __future__ import annotations

import os
import subprocess

HOST = os.environ.get('H3_HOST', '192.168.0.14')
PORT = os.environ.get('H3_SSH_PORT', '2222')
NEED_MIB = int(os.environ.get('H3_NEED_MIB', '14000'))   # 1344x768x192f が通る目安
# rqdb4ai が順番を管理している相手。これらが握っているのは想定内なので名前を出すだけ。
MANAGED = ('ollama', 'llama-server', 'ComfyUI', 'acestep', 'heartlib')


def _ssh(cmd: str) -> str:
    pw = ''
    try:
        import re
        mem = os.path.expanduser(
            '~/.claude/projects/-home-kojima-work/memory/reference_network_topology.md')
        with open(mem, encoding='utf-8') as f:
            m = re.search(r'パスワード[^`]*`([^`]*)`', f.read())
        pw = m.group(1) if m else ''
    except Exception:  # noqa: BLE001
        pw = ''
    base = ['ssh', '-p', PORT, '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=15',
            f'kojima@{HOST}', cmd]
    argv = (['sshpass', '-e'] + base) if pw else base
    env = dict(os.environ, SSHPASS=pw) if pw else os.environ
    r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
    return r.stdout.strip() if r.returncode == 0 else ''


def free_mib():
    """空き VRAM(MiB) と、いま GPU を使っているプロセスの一覧を返す。取れなければ (None, [])。"""
    out = _ssh('nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits; '
               'echo ---; nvidia-smi --query-compute-apps=used_memory,process_name '
               '--format=csv,noheader,nounits')
    if not out or '---' not in out:
        return None, []
    head, _, tail = out.partition('---')
    try:
        used, total = (int(x.strip()) for x in head.strip().splitlines()[0].split(','))
    except Exception:  # noqa: BLE001
        return None, []
    procs = []
    for line in tail.strip().splitlines():
        if ',' not in line:
            continue
        mib, name = line.split(',', 1)
        try:
            procs.append((int(mib.strip()), name.strip()))
        except ValueError:
            pass
    return total - used, sorted(procs, reverse=True)


def check_or_die(warn: bool = False) -> bool:
    free, procs = free_mib()
    if free is None:
        print('（0.14 の GPU を確認できませんでした。そのまま投入します）')
        return True
    print(f'0.14 の空きVRAM: {free:,} MiB（目安 {NEED_MIB:,} MiB 以上）')
    # **キューの外で握っているものだけを問題にする。** rqdb4ai が順番を管理している相手
    # （Ollama・ComfyUI・ACE-Step・HeartMuLa）は、H3 の番が来れば譲るので止めなくてよい。
    outside = [(mib, name) for mib, name in procs
               if not any(k.lower() in name.lower() for k in MANAGED)]
    for mib, name in procs[:6]:
        managed = not any(mib == m and name == n for m, n in outside)
        print(f'   {mib:>7,} MiB  {name}' + ('   （rqdb4ai が順番を管理）' if managed
                                             else '   ← キューの外'))
    if not outside:
        if free < NEED_MIB:
            print('   空きは少ないですが、握っているのは順番を管理している相手だけなので投入します。')
        return True
    total_outside = sum(m for m, _ in outside)
    print(f'!! キューの外のプロセスが {total_outside:,} MiB 握っています。'
          '投入前に止めてください（jevlocal などの常駐サービス）。')
    if warn:
        return False
    raise SystemExit('投入を中止しました（20分待ってから落ちるのを防ぐため）')


if __name__ == '__main__':
    check_or_die(warn=True)
