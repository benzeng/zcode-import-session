#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zcode-import-sessions.py — 把另一台机器备份的 ZCode 会话导入本机。

用法:
  python zcode-import-sessions.py <备份目录> [选项]

  备份目录结构需与 ~/.zcode 一致, 至少包含:
      cli/db/db.sqlite          会话主库 (session/message/part 等)
      v2/tasks-index.sqlite     任务索引 (界面"会话"列表的数据源)

选项:
  --map "旧路径=新路径"     工作目录映射, 可多次指定。Windows 路径建议整体加引号。
  --dry-run                只报告将要做什么, 不写任何文件 (退出码 2)
  --list                   仅列出备份中的会话, 不导入
  --skip-files             不复制 rollout/artifacts/image-cache 文件
  --home <dir>             本机 .zcode 目录 (默认 ~/.zcode)

流程:
  1. 预检: schema 版本对比、重复会话检测、目录结构检查、新路径存在性
  2. 备份本机 db.sqlite / tasks-index.sqlite / setting.json (sqlite backup API 一致性快照)
  3. 合并 db.sqlite: session/session_entry/message/part/session_input/todo/
     session_target/tool_usage/turn_usage/model_usage/input_history (INSERT OR IGNORE, 幂等)
  4. 合并 v2/tasks-index.sqlite: tasks 表
  5. 复制文件: cli/rollout/model-io-sess_*.jsonl, cli/artifacts/sess_*/,
     cli/image-cache/sess_*/  (已存在则跳过)
  6. 路径改写 (--map): session.directory/path/project_id;
     JSON blob 列 (2/4/6/8 层反斜杠转义 + 正斜杠 + MSYS /f/ 形式);
     tasks 表 workspace_key/workspace_path/meta_json/searchable_text;
     只匹配带盘符的完整路径, 对话正文中单独出现的文件夹名不会误改
  7. 把新工作目录加入 v2/setting.json 的 recentProjects (排重, 上限 10)
  8. 验证: 行数对比 / 残留旧路径扫描 / JSON 有效性 / quick_check, 输出汇总

有意不迁移 (跨机无意义或会撞号):
  workflow_* / session_task_link (子代理工作流, 常规会话为空)
  v2/checkpoints (工作区快照, 绑定原机器加密密钥)
  local_setting / permission (本机设置)
  cli/exec (运行时临时状态)
  session_input 中 runtime_command_N 行 (全局自增 id 跨机必撞号, 自动跳过, 不含对话内容)

导入后须知:
  * 完全退出 ZCode (托盘右键退出; 仅关窗口是最小化到托盘) 再启动
  * 侧栏"项目"区 = 当前打开的工作区标签页, 不是"所有有会话的目录":
    打开一次新目录 (欢迎页"最近项目"可直接点) 它才出现在"项目"区, 其下即列出导入的会话
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

BS = chr(92)  # 反斜杠, 统一用变量拼接, 避免源码转义混乱

# db.sqlite 中按 session 关联的表: (表名, 会话id列)
SESSION_TABLES = [
    ('session', 'id'),
    ('session_entry', 'session_id'),
    ('message', 'session_id'),
    ('part', 'session_id'),
    ('session_input', 'session_id'),
    ('todo', 'session_id'),
    ('session_target', 'session_id'),
    ('tool_usage', 'session_id'),
    ('turn_usage', 'session_id'),
    ('model_usage', 'session_id'),
    ('input_history', 'session_id'),
]

# 做路径改写的 JSON/文本 blob 列
BLOB_COLUMNS = [
    ('message', 'data'),
    ('part', 'data'),
    ('session_input', 'payload'),
    ('session_entry', 'data'),
    ('input_history', 'text'),
    ('input_history', 'attachments'),
    ('todo', 'content'),
]

TASKS_TEXT_COLUMNS = ['meta_json', 'searchable_text']


def die(msg):
    print(f'[错误] {msg}', file=sys.stderr)
    sys.exit(1)


def project_id_from_path(path):
    # 约定: proj_ + 小写路径, 去掉 ':', '\' 与 '/' 变 '-'
    # 规则实测自 ZCode 0.16.x: 例 E:\work\demo -> proj_e-work-demo
    return 'proj_' + path.lower().replace(':', '').replace(BS, '-').replace('/', '-')


def path_forms(old, new):
    """(旧形式, 新形式) 替换对: 多层 JSON 反斜杠转义 + 正斜杠 + MSYS 风格。
    各形式互斥 (盘符 + 精确反斜杠个数锚定), 顺序无关。"""
    pairs = []
    for nb in (2, 4, 6, 8):
        pairs.append((old.replace(BS, BS * nb), new.replace(BS, BS * nb)))
    pairs.append((old.replace(BS, '/'), new.replace(BS, '/')))                  # F:/a/b
    for d_old, d_new in ((old[0].lower(), new[0].lower()), (old[0].upper(), new[0].upper())):
        pairs.append(('/' + d_old + old[1:].replace(BS, '/'),
                      '/' + d_new + new[1:].replace(BS, '/')))                  # /f/a/b, /F/a/b
    pairs.append((old, new))                                                    # 单反斜杠明文
    seen, uniq = set(), []
    for p in pairs:
        if p[0] not in seen:
            seen.add(p[0])
            uniq.append(p)
    return uniq


def table_columns(con, table):
    return [r[1] for r in con.execute(f'PRAGMA table_info({table})')]


def merge_table(dst, bak_path, table, key_col, sids, transforms=None):
    """按列交集 INSERT OR IGNORE; 返回 (插入行数, 列交集)。dst 需已 ATTACH bak 为 src。
    transforms: {列名: [(旧,新), ...]} — 在 SELECT 阶段做 REPLACE 链 (用于插入时翻译
    组成主键的路径列, 保证与已改写行正确冲突去重)。"""
    local_cols = table_columns(dst, table)
    src_con = sqlite3.connect(f'file:{bak_path}?mode=ro', uri=True)
    src_cols = set(table_columns(src_con, table))
    src_con.close()
    cols = [c for c in local_cols if c in src_cols]
    if not cols:
        return 0, []
    q = ','.join('?' * len(sids))
    sel_cols, sel_params = [], []
    for c in cols:
        expr = f'src.{table}.{c}'
        for old, new in (transforms or {}).get(c, []):
            expr = f'REPLACE({expr}, ?, ?)'
            sel_params.extend([old, new])
        sel_cols.append(expr)
    collist = ','.join(cols)
    sellist = ','.join(sel_cols)
    n = dst.execute(
        f'INSERT OR IGNORE INTO main.{table} ({collist}) '
        f'SELECT {sellist} FROM src.{table} WHERE src.{table}.{key_col} IN ({q})',
        sel_params + sids).rowcount
    return n, cols


def snapshot_db(src_path, dst_path):
    s = sqlite3.connect(src_path)
    d = sqlite3.connect(dst_path)
    s.backup(d)
    d.close()
    s.close()


def sql_quote(path):
    return path.replace(chr(39), chr(39) * 2)


def main():
    ap = argparse.ArgumentParser(description='导入另一台机器备份的 ZCode 会话')
    ap.add_argument('backup', help='备份目录 (含 cli/ 与 v2/)')
    ap.add_argument('--map', action='append', default=[], metavar='OLD=NEW',
                    help='工作目录映射, 可多次指定')
    ap.add_argument('--dry-run', action='store_true', help='只报告, 不写入')
    ap.add_argument('--list', action='store_true', help='仅列出备份中的会话')
    ap.add_argument('--skip-files', action='store_true', help='不复制会话附属文件')
    ap.add_argument('--home', default=os.path.join(os.path.expanduser('~'), '.zcode'),
                    help='本机 .zcode 目录 (默认 ~/.zcode)')
    args = ap.parse_args()

    bak_root = os.path.abspath(args.backup)
    home = os.path.abspath(args.home)
    bak_db = os.path.join(bak_root, 'cli', 'db', 'db.sqlite')
    bak_tasks = os.path.join(bak_root, 'v2', 'tasks-index.sqlite')
    loc_db = os.path.join(home, 'cli', 'db', 'db.sqlite')
    loc_tasks = os.path.join(home, 'v2', 'tasks-index.sqlite')
    loc_setting = os.path.join(home, 'v2', 'setting.json')
    dry = args.dry_run
    verb = '将插入' if dry else '插入'

    if bak_root == home:
        die('备份目录不能就是本机 .zcode 目录')
    for p, desc in [(bak_db, '备份 cli/db/db.sqlite'), (bak_tasks, '备份 v2/tasks-index.sqlite')]:
        if not os.path.isfile(p):
            die(f'找不到 {desc}: {p}')
    for p, desc in [(loc_db, '本机 cli/db/db.sqlite'), (loc_tasks, '本机 v2/tasks-index.sqlite')]:
        if not os.path.isfile(p):
            die(f'找不到 {desc}: {p}')

    # ---- 解析目录映射 ----
    mappings = []
    for m in args.map:
        if '=' not in m:
            die(f'--map 格式应为 "旧路径=新路径", 收到: {m}')
        old, new = m.split('=', 1)
        old, new = old.strip().rstrip(BS + '/'), new.strip().rstrip(BS + '/')
        if not os.path.isabs(old) or not os.path.isabs(new):
            die(f'--map 需要绝对路径: {m}')
        mappings.append((old, new))
    # 长路径优先: 当映射间存在前缀包含关系时, 先替换更具体的路径
    mappings.sort(key=lambda t: len(t[0]), reverse=True)

    # ---- 读取备份会话清单 ----
    bak_ro = sqlite3.connect(f'file:{bak_db}?mode=ro', uri=True)
    sessions = bak_ro.execute(
        'SELECT id, title, directory, time_created FROM session ORDER BY time_created').fetchall()
    sids = [s[0] for s in sessions]
    q = ','.join('?' * len(sids))

    if mappings:
        old_set = {old for old, _new in mappings}
        unmapped = sorted({d for _i, _t, d, _c in sessions if d not in old_set})
        if unmapped:
            print('[警告] 以下会话目录不在任何 --map 中, 将按原路径导入 (不影响导入本身):')
            for d in unmapped:
                print(f'    {d}')

    if args.list:
        info = print
        info(f'备份 {bak_root} 共 {len(sessions)} 个会话:')
        for sid, title, directory, tc in sessions:
            nmsg = bak_ro.execute('SELECT COUNT(*) FROM message WHERE session_id=?', (sid,)).fetchone()[0]
            info(f'  [{time.strftime("%Y-%m-%d", time.localtime(tc / 1000))}] {title}  ({nmsg} 条消息)')
            info(f'      {sid}')
            info(f'      目录: {directory}')
        bak_ro.close()
        return

    # ---- 预检 ----
    print('=' * 62)
    print('预检')
    print('=' * 62)
    print(f'备份: {bak_root}')
    print(f'本机: {home}')
    for old, new in mappings:
        mark = '' if os.path.isdir(new) else '  [警告: 本机不存在, 可导入但续聊时找不到项目文件]'
        print(f'目录映射: {old} -> {new}{mark}')
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ZCode.exe'],
                             capture_output=True).stdout
        if b'ZCode.exe' in out:
            print('[提示] ZCode 正在运行: 合并可安全进行 (WAL+短事务), 完成后需完全退出再启动')
    except Exception:
        pass
    bak_mig = bak_ro.execute('SELECT id, checksum FROM schema_migration ORDER BY id').fetchall()
    bak_ro.close()
    loc_ro = sqlite3.connect(f'file:{loc_db}?mode=ro', uri=True)
    loc_mig = loc_ro.execute('SELECT id, checksum FROM schema_migration ORDER BY id').fetchall()
    dup = [s for s in sids if loc_ro.execute('SELECT 1 FROM session WHERE id=?', (s,)).fetchone()]
    loc_ro.close()
    if bak_mig != loc_mig:
        b = {m[0]: m[1] for m in bak_mig}
        l = {m[0]: m[1] for m in loc_mig}
        only_bak = set(b) - set(l)
        diff = [k for k in set(b) & set(l) if b[k] != l[k]]
        if only_bak:
            die(f'备份 schema 比本机新 ({sorted(only_bak)}), 请先升级本机 ZCode')
        if diff:
            die(f'schema 校验和不一致: {diff}')
        print(f'[提示] 本机 schema 更新 ({sorted(set(l) - set(b))}), 备份行仍可导入')
    else:
        print('schema 版本一致: OK')
    print(f'备份会话数: {len(sids)}, 本机已存在(幂等补齐): {len(dup)}')
    print()

    # ---- 备份本机数据 ----
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bakout = os.path.join(home, 'import-backups', stamp)
    if dry:
        print(f'[dry-run] 将备份本机数据到 {bakout}')
    else:
        os.makedirs(bakout, exist_ok=True)
        snapshot_db(loc_db, os.path.join(bakout, 'db.sqlite'))
        snapshot_db(loc_tasks, os.path.join(bakout, 'tasks-index.sqlite'))
        if os.path.isfile(loc_setting):
            shutil.copy2(loc_setting, os.path.join(bakout, 'setting.json'))
        print(f'已备份本机数据到 {bakout}')

    # ---- 合并 db.sqlite ----
    print()
    print('=' * 62)
    print('合并会话库 cli/db/db.sqlite')
    print('=' * 62)
    con = sqlite3.connect(loc_db, timeout=30)
    con.execute('PRAGMA busy_timeout=30000')
    con.execute(f"ATTACH DATABASE '{sql_quote(bak_db)}' AS src")
    total = 0
    try:
        con.execute('BEGIN IMMEDIATE')
        for table, key in SESSION_TABLES:
            try:
                n, _cols = merge_table(con, bak_db, table, key, sids)
            except sqlite3.OperationalError as e:
                print(f'  {table}: 跳过 ({e})')
                continue
            total += n
            print(f'  {table}: {verb} {n} 行')
        if dry:
            con.rollback()
            print(f'[dry-run] 合计将插入 {total} 行 (已回滚)')
        else:
            con.commit()
            print(f'合计插入 {total} 行')
    except Exception:
        con.rollback()
        raise
    finally:
        con.execute('DETACH DATABASE src')
        con.close()

    # ---- 合并 tasks-index ----
    print()
    print('=' * 62)
    print('合并任务索引 v2/tasks-index.sqlite')
    print('=' * 62)
    ti = sqlite3.connect(loc_tasks, timeout=30)
    ti.execute('PRAGMA busy_timeout=30000')
    ti.execute(f"ATTACH DATABASE '{sql_quote(bak_tasks)}' AS src")
    try:
        ti.execute('BEGIN IMMEDIATE')
        # tasks 主键含 workspace_key: 插入阶段即翻译路径, 保证重复运行时正确冲突去重
        task_transforms = {}
        if mappings:
            for col in ['workspace_key', 'workspace_path'] + TASKS_TEXT_COLUMNS:
                task_transforms[col] = [p for old, new in mappings for p in path_forms(old, new)]
        n, _ = merge_table(ti, bak_tasks, 'tasks', 'task_id', sids, task_transforms)
        # 自愈: 清掉同 task_id 残留旧 workspace_key 的行 (历史部分运行可能留下)
        for old, _new in mappings:
            stale = ti.execute(
                f'DELETE FROM main.tasks WHERE task_id IN ({q}) AND workspace_key = ?',
                sids + [old]).rowcount
            if stale:
                print(f'  清理旧键残留行: {stale}')
        if dry:
            ti.rollback()
            print(f'[dry-run] tasks: 将插入 {n} 行 (已回滚)')
        else:
            ti.commit()
            print(f'tasks: 插入 {n} 行')
    except Exception:
        ti.rollback()
        raise
    finally:
        ti.execute('DETACH DATABASE src')
        ti.close()

    # ---- 复制附属文件 ----
    print()
    print('=' * 62)
    print('复制会话附属文件 (rollout / artifacts / image-cache)')
    print('=' * 62)
    if args.skip_files:
        print('  [跳过] (--skip-files)')
    else:
        jobs = []
        for sid in sids:
            rollout = os.path.join(bak_root, 'cli', 'rollout', f'model-io-{sid}.jsonl')
            if os.path.isfile(rollout):
                jobs.append((rollout, os.path.join(home, 'cli', 'rollout')))
            for sub in ('artifacts', 'image-cache'):
                d = os.path.join(bak_root, 'cli', sub, sid)
                if os.path.isdir(d):
                    jobs.append((d, os.path.join(home, 'cli', sub)))
        copied = skipped = 0
        for src_path, dstdir in jobs:
            dst = os.path.join(dstdir, os.path.basename(src_path))
            if os.path.exists(dst):
                skipped += 1
                continue
            if not dry:
                os.makedirs(dstdir, exist_ok=True)
                (shutil.copy2 if os.path.isfile(src_path) else shutil.copytree)(src_path, dst)
            copied += 1
        print(f'  {"将复制" if dry else "复制"} {copied} 项, 已存在跳过 {skipped} 项')

    # ---- 路径改写 ----
    if mappings:
        print()
        print('=' * 62)
        print('改写工作目录路径')
        print('=' * 62)
        q = ','.join('?' * len(sids))
        if dry:
            print('  [dry-run] 将改写: session 目录字段 + JSON blob 列 + tasks workspace/meta/搜索文本')
        else:
            con = sqlite3.connect(loc_db, timeout=30)
            con.execute('PRAGMA busy_timeout=30000')
            try:
                con.execute('BEGIN IMMEDIATE')
                for old, new in mappings:
                    n = con.execute(
                        f'UPDATE session SET directory=?, path=?, project_id=? '
                        f'WHERE id IN ({q}) AND (directory=? OR path=?)',
                        [new, new, project_id_from_path(new)] + sids + [old, old]).rowcount
                    print(f'  session 目录字段 ({old} -> {new}): {n} 行')
                    for table, col in BLOB_COLUMNS:
                        hit = 0
                        for o, nw in path_forms(old, new):
                            hit += con.execute(
                                f"UPDATE {table} SET {col}=REPLACE({col},?,?) "
                                f"WHERE session_id IN ({q}) AND {col} LIKE ?",
                                [o, nw] + sids + [f'%{o}%']).rowcount
                        if hit:
                            print(f'  {table}.{col}: {hit} 行更新')
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()
            ti = sqlite3.connect(loc_tasks, timeout=30)
            ti.execute('PRAGMA busy_timeout=30000')
            try:
                ti.execute('BEGIN IMMEDIATE')
                for old, new in mappings:
                    n = ti.execute(
                        f'UPDATE OR IGNORE tasks SET workspace_key=?, workspace_path=? '
                        f'WHERE task_id IN ({q}) AND (workspace_key=? OR workspace_path=?)',
                        [new, new] + sids + [old, old]).rowcount
                    print(f'  tasks workspace 字段: {n} 行')
                    for col in TASKS_TEXT_COLUMNS:
                        hit = 0
                        for o, nw in path_forms(old, new):
                            hit += ti.execute(
                                f"UPDATE tasks SET {col}=REPLACE({col},?,?) "
                                f"WHERE task_id IN ({q}) AND {col} LIKE ?",
                                [o, nw] + sids + [f'%{o}%']).rowcount
                        if hit:
                            print(f'  tasks.{col}: {hit} 行更新')
                ti.commit()
            except Exception:
                ti.rollback()
                raise
            finally:
                ti.close()

    # ---- recentProjects ----
    new_paths = [new for _o, new in mappings if os.path.isdir(new)]
    if new_paths and os.path.isfile(loc_setting):
        print()
        print('=' * 62)
        print('注册到 recentProjects (v2/setting.json)')
        print('=' * 62)
        raw = open(loc_setting, encoding='utf-8').read()
        setting = json.loads(raw)
        rp = setting.get('recentProjects', [])
        changed = False
        for p in new_paths:
            if p not in rp:
                rp.insert(0, p)
                changed = True
        rp = list(dict.fromkeys(rp))[:10]
        if not changed:
            print('  recentProjects 已包含目标路径, 无需修改')
        elif dry:
            print(f'  [dry-run] 将把 {new_paths} 插入 recentProjects 首位')
        else:
            setting['recentProjects'] = rp
            shutil.copy2(loc_setting, os.path.join(bakout, 'setting.pre-import.json'))
            with open(loc_setting, 'w', encoding='utf-8', newline='') as f:
                f.write(json.dumps(setting, ensure_ascii=False, indent=2) +
                        ('\n' if raw.endswith('\n') else ''))
            print(f'  已更新: {json.dumps(rp, ensure_ascii=False)}')

    # ---- 验证与汇总 ----
    print()
    print('=' * 62)
    print('验证与汇总')
    print('=' * 62)
    q = ','.join('?' * len(sids))
    bak_ro = sqlite3.connect(f'file:{bak_db}?mode=ro', uri=True)
    loc_ro = sqlite3.connect(f'file:{loc_db}?mode=ro', uri=True)
    if dry:
        print('[dry-run] 行数对比为"导入前"状态, 仅示意')
    anomaly = False
    for table, key in SESSION_TABLES:
        b = bak_ro.execute(f'SELECT COUNT(*) FROM {table} WHERE {key} IN ({q})', sids).fetchone()[0]
        l = loc_ro.execute(f'SELECT COUNT(*) FROM {table} WHERE {key} IN ({q})', sids).fetchone()[0]
        note = ''
        if b != l:
            if table == 'session_input':
                note = '  (runtime_command_N 全局 id 撞号, 属预期)'
            else:
                note = '  [不一致!]'
                anomaly = True
        print(f'  {table}: 备份 {b} / 本机 {l}{note}')
    if mappings and not dry:
        # 残留旧路径扫描 (仅统计仍含旧路径完整形式的行, 项目外路径不算)
        for old, new in mappings:
            forms = [o for o, _n in path_forms(old, new)]
            left = 0
            for table, col in BLOB_COLUMNS:
                for o in forms:
                    left += loc_ro.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE session_id IN ({q}) AND {col} LIKE ?",
                        sids + [f'%{o}%']).fetchone()[0]
            print(f'  残留旧路径 {old}: {left} 行' + ('  [需检查]' if left else '  无'))
            if left:
                anomaly = True
        bad = 0
        for table, col in [('message', 'data'), ('part', 'data'), ('session_input', 'payload')]:
            for (txt,) in loc_ro.execute(
                    f'SELECT {col} FROM {table} WHERE session_id IN ({q})', sids):
                try:
                    json.loads(txt)
                except Exception:
                    bad += 1
        print(f'  JSON 有效性: {"全部通过" if bad == 0 else f"{bad} 个损坏!"}')
        if bad:
            anomaly = True
    print(f'  db.sqlite quick_check: {loc_ro.execute("PRAGMA quick_check").fetchone()[0]}')
    print()
    print(f'导入的会话 ({len(sids)} 个):')
    for sid, title, _directory, tc in sessions:
        row = loc_ro.execute('SELECT directory FROM session WHERE id=?', (sid,)).fetchone()
        nmsg = loc_ro.execute('SELECT COUNT(*) FROM message WHERE session_id=?', (sid,)).fetchone()[0]
        print(f'  [{time.strftime("%Y-%m-%d", time.localtime(tc / 1000))}] {title}  ({nmsg} 条消息)')
        print(f'      -> {row[0] if row else "?"}')
    bak_ro.close()
    loc_ro.close()

    print()
    print('后续步骤:')
    print('  1. 完全退出 ZCode (托盘右键退出) 并重新启动')
    print('  2. 在欢迎页"最近项目"打开新目录 (或用"打开文件夹"), 它才会出现在侧栏"项目"区')
    print('     —— "项目"区列出的是当前打开的工作区标签页, 不是所有有会话的目录')
    if anomaly:
        print()
        print('[注意] 存在需检查项, 见上方标记。出错时可用 import-backups 下的快照还原。')
    if dry:
        sys.exit(2)


if __name__ == '__main__':
    main()
