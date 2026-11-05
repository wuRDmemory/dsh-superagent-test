#!/usr/bin/env python3
"""Daily 09:00 Feishu document digest.

Scans all chats for documents shared yesterday (Asia/Shanghai), classifies them
into the 3-level schema, appends new records to the Base, and notifies the user.
"""
import argparse, json, os, re, subprocess, sys, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault('FEISHU_CHAT_CACHE', os.path.join(HERE, 'chats.json'))
import feishu_scan_lib as lib

TZ = timezone(timedelta(hours=8))
CONFIG_PATH = os.environ.get('FEISHU_DAILY_CONFIG', os.path.join(HERE, 'config.json'))
CFG = {}
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, encoding='utf-8') as f:
        CFG = json.load(f)
BASE_TOKEN = CFG.get('base_token', '')
TABLE_ID = CFG.get('table_id', '')
BASE_URL = CFG.get('base_url') or (('https://feishu.cn/base/' + BASE_TOKEN) if BASE_TOKEN else '')
CHAT_ID = CFG.get('chat_id', '')
BASE_OPTIONS = {}


def load_base_options():
    """Read select options from the Base so new values never break a write."""
    try:
        data = lib.run(['base', '+field-list', '--base-token', BASE_TOKEN, '--table-id', TABLE_ID, '--as', 'user'], timeout=60)
    except Exception as e:
        log(f'could not load Base field options: {e}')
        return
    for f in data.get('fields') or []:
        if f.get('type') == 'select':
            BASE_OPTIONS[f.get('name')] = {o.get('name') for o in f.get('options') or []}


def norm_option(field, value, fallback):
    opts = BASE_OPTIONS.get(field)
    if not opts or value in opts:
        return value
    return fallback
STATE_PATH = os.path.join(HERE, 'seen.json')

FIN_KW = ['dzfp', '发票', '付款', '银行', '贷款', '税务', '估值', '尽调', '货款', '报销', '资金', '融资', '投资人', '合同', '回款', '预算', '成本', '财务']
DATA_KW = ['总表', '汇总', '台账', '记录表', '盘点', 'BOM', 'SKU', 'SN划分', '预留量', '负载搜集', 'return_selection', '测算', '经营分析', '控制表', '跟踪表']
RESUME_KW = ['简历', 'CV', 'cv', '候选人', '应届生', '实习生']
RESUME_RE = re.compile(r'【[^】]*(工程师|算法|开发|产品|运营|实习|岗位|招聘)[^】]*】.*\d+\s*年.*\.(pdf|docx?)$', re.I)


def log(msg):
    print(f"[{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def excluded(title, chat):
    t = (title or '') + ' ' + (chat or '')
    if any(k in t for k in FIN_KW):
        return True
    if any(k in t for k in DATA_KW):
        return True
    if re.search(r'_\d+\.\d{2}\.pdf$', t):
        return True
    # resumes / candidate CVs
    if any(k in t for k in RESUME_KW) or RESUME_RE.search(title or ''):
        return True
    if ('面试' in (chat or '') or '招聘' in (chat or '')) and re.search(r'\.(pdf|docx|doc)$', title or '', re.I):
        return True
    return False


def doc_type(title, chat):
    t = (title or '') + ' ' + (chat or '')
    if any(k in t for k in ('会议', '纪要', '周会', '站会', '协调会', '研讨会', '沟通会', '讨论会', '评审会', '例会', 'triage', 'meeting', 'standup')):
        return '会议纪要'
    if any(k in t for k in ('8D', '进水', '停机', '失效', '客诉', '返工', '报修', '报错', '故障', '异常', '问题')):
        return '问题总结'
    if any(k in t for k in ('认证', '安规', '可靠性', '合规')):
        return '认证/安规'
    if any(k in t for k in ('测试', '调试', '验证', '用例', '产测', '测试报告')):
        return '测试文档'
    if any(k in t for k in ('需求', 'PRD', 'prd', '型号', '版本管控', '衍生型号', '功能')):
        return '需求文档'
    if any(k in t for k in ('方案', '设计', '结构', '图纸', '规格', 'spec', 'Specification', 'DFM', 'dfm', '模具', '工艺')):
        return '方案/设计'
    if any(k in t for k in ('计划', '规划', '周报', '汇报', 'GTM', '排期', '进展', '复盘')):
        return '项目计划/汇报'
    if any(k in t for k in ('流程', '制度', '规范', '权限', 'SOP', '模板', '指引')):
        return '流程/制度'
    if any(k in t for k in ('招聘', '面试', '绩效', '培训', '员工', '人力', '组织')):
        return '人事/招聘'
    if any(k in t for k in ('手册', '指南', '标准', '资料', '知识', 'Logo', 'logo')):
        return '知识/资料'
    return '其他/待归类'


def dept(title, chat):
    t = (title or '') + ' ' + (chat or '')
    if any(k in t for k in ('结构', '模具', '塑胶', '防水', '外壳', '履带', '刀头', '水泵', '工艺')):
        return '结构部'
    if any(k in t for k in ('硬件', '电机', '轮毂', 'PCB', 'pcb', '电路', '板', '散热', '器件', '电源', '充电器', '适配器')):
        return '硬件部'
    if any(k in t for k in ('算法', '感知', 'SLAM', 'RTK', 'NRTK', 'GNSS', '导航', '定位')):
        return '算法部'
    if any(k in t for k in ('软件', '固件', '通讯', '版本', '4G', '嵌入式', 'APP', '上位机', '发版', '用户与权限')):
        return '软件部'
    if any(k in t for k in ('测试', '验证', '问题', '失效', '8D', '产测', '调试')):
        return '测试部'
    if any(k in t for k in ('项目', '评审', '计划', '进展', '周会', '规划')):
        return '项目部'
    if any(k in t for k in ('供应链', '采购', '物流', '生产', '制造', '备料', '物料', '供应商')):
        return '供应链部'
    if any(k in t for k in ('市场', '品牌', 'Logo', 'KOL', '亚马逊', '独立站', '营销', '销售')):
        return '市场部'
    if any(k in t for k in ('招聘', '面试', '人力', '绩效', '员工', '培训')):
        return '人力资源'
    if any(k in t for k in ('客户', '售后', '客诉')):
        return '客户服务部'
    if any(k in t for k in ('认证', '安规')):
        return '认证部'
    if any(k in t for k in ('产品', '需求', 'PRD')):
        return '产品部'
    return '其他/待归类'


def project(title, chat):
    t = ((title or '') + ' ' + (chat or '')).lower()
    for p in CFG.get('projects', []):
        if any(str(k).lower() in t for k in p.get('keywords', [])):
            return p.get('name') or '未命名项目'
    return CFG.get('default_project', '通用/公司')


def tags_for(title, doctype):
    t = title or ''
    out = []
    if doctype == '会议纪要': out.append('会议')
    if any(k in t for k in ('评审', 'review', 'Review')): out.append('评审')
    if any(k in t for k in ('认证', '安规')): out.append('认证/安规')
    if any(k in t for k in ('ECN', 'ecn', '变更')): out.append('ECN/变更')
    if any(k in t for k in ('客户', '客诉', '售后', '退换货')): out.append('客户/售后')
    if any(k in t for k in ('供应商', '采购', '物料')): out.append('供应商/物料')
    if any(k in t for k in ('模板', '协议', '规范', '制度')): out.append('模板/制度')
    if any(k in t for k in ('版本', '发布', 'GTM', '量产')): out.append('版本/发布')
    return list(dict.fromkeys(out))


def fmt_of(d):
    if d['kind'] == 'file':
        ext = (d.get('ext') or '').lower()
        return {'.pdf': 'PDF', '.xlsx': 'Excel', '.xls': 'Excel', '.docx': 'Word', '.doc': 'Word', '.pptx': 'PPT', '.ppt': 'PPT', '.csv': 'CSV', '.md': 'Markdown', '.txt': '文本', '.zip': '压缩包'}.get(ext, ext.upper().lstrip('.') or '文件')
    return {'DOCX': '飞书文档', 'DOC': '飞书文档', 'SHEET': '飞书表格', 'BITABLE': '多维表格', 'SLIDES': '幻灯片', 'MINDNOTE': '思维笔记', 'FILE': '文件', 'WIKI': 'Wiki 页面'}.get(d.get('type'), d.get('type') or '文档')


def key_of(d):
    if d['kind'] == 'file':
        return 'file:' + str(d.get('token') or '') + ':' + str(d.get('file_name') or '')
    return 'link:' + str(d.get('type') or '') + ':' + str(d.get('token') or '')


def parse_url(u):
    m = re.match(r'https?://[^/]+/(docx|docs|wiki|sheets|base|slides|mindnotes|file|drive/folder)/([A-Za-z0-9_-]+)', u or '')
    if not m: return None
    return {'docx':'0', 'docs':'1', 'wiki':'2', 'sheets':'3', 'base':'4', 'slides':'5', 'mindnotes':'6', 'file':'7', 'drive/folder':'8'}[m.group(1)], m.group(2)


def enrich_titles(docs):
    """Batch-resolve link doc titles via drive metas. Mutates docs."""
    req, mapping = [], {}
    dtmap = {'0':'docx','1':'doc','2':'wiki','3':'sheet','4':'bitable','5':'slides','6':'mindnote','7':'file','8':'folder'}
    for d in docs:
        if d['kind'] != 'link': continue
        pu = parse_url(d.get('url'))
        if not pu: continue
        kind, token = pu
        dt = dtmap[kind]
        req.append({'doc_token': token, 'doc_type': dt})
        mapping[(dt, token)] = d
    if not req:
        return
    data = lib.run(['drive', 'metas', 'batch_query', '--data', json.dumps({'request_docs': req, 'with_url': True}), '--as', 'user'], timeout=180)
    for m in data.get('metas') or []:
        key = ((m.get('doc_type') or '').lower(), m.get('doc_token'))
        d = mapping.get(key)
        if d and m.get('title'):
            d['title'] = m['title']


def load_state():
    if os.path.exists(STATE_PATH):
        return json.load(open(STATE_PATH, encoding='utf-8'))
    return {'seen': {}, 'record_map': {}}


def save_state(state):
    tmp = STATE_PATH + '.tmp'
    json.dump(state, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def notify(text, dry=False):
    if dry:
        log('DRY RUN notification: ' + text)
        return
    if os.environ.get('FEISHU_DAILY_NO_NOTIFY'):
        log('NOTIFY (suppressed): ' + text)
        return
    last = None
    for attempt in range(3):
        try:
            lib.run(['im', '+messages-send', '--chat-id', CHAT_ID, '--as', 'bot', '--markdown', text], timeout=60)
            return
        except Exception as e:
            last = e
            log(f'notification failed (attempt {attempt+1}/3): {e}')
            time.sleep(10 + attempt * 10)
    log(f'notification failed after retries: {last}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default='', help='YYYY-MM-DD; default yesterday')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit-chats', type=int, default=0)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--max-pages', type=int, default=20)
    args = ap.parse_args()
    if not CFG:
        raise SystemExit(f"config not found: {CONFIG_PATH}; copy config.example.json to config.json and fill it in")
    load_base_options()

    now = datetime.now(TZ)
    if args.date:
        target = datetime.strptime(args.date, '%Y-%m-%d').replace(tzinfo=TZ)
    else:
        target = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start = target.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    start_s, end_s = int(start.timestamp()), int(end.timestamp())
    log(f'daily run for {start.date()} (window {start.isoformat()} .. {end.isoformat()})')

    # auth / chats
    try:
        chats = lib.list_chats()
    except Exception as e:
        notify(f'早上好，今天的文档自动整理失败：无法读取会话列表（{e}）。请检查飞书授权。')
        raise
    if args.limit_chats:
        chats = chats[:args.limit_chats]
    log(f'chats={len(chats)}')

    chat_name = {c.get('chat_id'): c.get('name') or c.get('chat_id') for c in chats}
    chat_mode = {c.get('chat_id'): c.get('chat_mode') or '' for c in chats}
    all_msgs, errors = [], []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(lib.fetch_chat, c, start_s, end_s, args.max_pages): c for c in chats}
        for fut in as_completed(futs):
            c = futs[fut]
            items, pages, err = fut.result()
            done += 1
            if err:
                errors.append({'chat_id': c.get('chat_id'), 'name': c.get('name'), 'error': err})
            else:
                all_msgs.extend(items)
            if done % 100 == 0:
                log(f'scanned {done}/{len(chats)} chats, messages={len(all_msgs)}, errors={len(errors)}')

    log(f'messages={len(all_msgs)} errors={len(errors)}')
    # brief pause so a long scan does not collide with the write/notify rate limit
    time.sleep(2)
    # extract unique docs with occurrence counts
    per_key = {}
    for m in all_msgs:
        cid = m.get('chat_id') or ''
        base = {'chat_id': cid, 'chat_name': chat_name.get(cid, cid), 'chat_mode': chat_mode.get(cid, ''),
                'message_id': m.get('message_id'), 'msg_type': m.get('msg_type'),
                'sender_id': (m.get('sender') or {}).get('id') or '',
                'create_time': m.get('create_time'), 'date': start.strftime('%Y-%m-%d')}
        links, files = lib.extract(m)
        seen_in_msg = set()
        for typ, tok, url in links:
            k = 'link:' + typ + ':' + tok
            if k in seen_in_msg: continue
            seen_in_msg.add(k)
            d = per_key.setdefault(k, {**base, 'kind':'link', 'type':typ, 'token':tok, 'url':url, 'file_name':'', 'ext':'', 'occurrences':0})
            d['occurrences'] += 1
        for f in files:
            if not f.get('is_doc'): continue
            k = 'file:' + str(f.get('file_key') or '') + ':' + str(f.get('file_name') or '')
            if k in seen_in_msg: continue
            seen_in_msg.add(k)
            d = per_key.setdefault(k, {**base, 'kind':'file', 'type':'FILE', 'token':f.get('file_key'), 'url':'', 'file_name':f.get('file_name') or '', 'ext':f.get('ext') or '', 'occurrences':0})
            d['occurrences'] += 1

    state = load_state()
    seen = state.setdefault('seen', {})
    record_map = state.setdefault('record_map', {})
    candidates = []
    for k, d in per_key.items():
        if k in seen:
            continue
        title = d.get('file_name') or d.get('url') or ''
        if excluded(title, d['chat_name']):
            continue
        candidates.append((k, d))
    # enrich link titles
    enrich_titles([d for _, d in candidates])
    new_docs = []
    for k, d in candidates:
        title = d.get('title') or d.get('file_name') or d.get('url') or '（无标题文档）'
        if excluded(title, d['chat_name']):
            continue
        # never index the knowledge Base into itself
        if d.get('url') and BASE_TOKEN and BASE_TOKEN in d['url']:
            continue
        dt = norm_option('文档类型', doc_type(title, d['chat_name']), '其他/待归类')
        dp = norm_option('部门', dept(title, d['chat_name']), '其他/待归类')
        pj = norm_option('项目', project(title, d['chat_name']), CFG.get('default_project', '通用/公司'))
        fmt = norm_option('格式', fmt_of(d), '文件')
        tg = tags_for(title, dt)
        known_tags = BASE_OPTIONS.get('标签')
        if known_tags:
            tg = [t for t in tg if t in known_tags]
        mode = '群聊' if d['chat_mode'] == 'group' else ('私聊' if d['chat_mode'] == 'p2p' else '话题群')
        mode = norm_option('来源', mode, '群聊')
        d.update({'title': title, 'doctype': dt, 'dept': dp, 'project': pj, 'fmt': fmt, 'tags': tg, 'mode': mode})
        new_docs.append((k, d))
    # documents already in the table (initial snapshot or previous daily runs)
    missing_title = [d for k, d in per_key.items()
                     if k in seen and d['kind'] == 'link' and not (seen.get(k) or {}).get('title')]
    if missing_title:
        enrich_titles(missing_title)
    existing_infos = []
    for k, d in per_key.items():
        if k not in seen:
            continue
        title = (seen.get(k) or {}).get('title') or d.get('title') or d.get('file_name') or d.get('url') or '（无标题文档）'
        if excluded(title, d['chat_name']):
            continue
        if d.get('url') and BASE_TOKEN and BASE_TOKEN in d['url']:
            continue
        dt = doc_type(title, d['chat_name'])
        existing_infos.append((k, {**d, 'title': title, 'doctype': dt, 'dept': dept(title, d['chat_name']),
                                   'project': project(title, d['chat_name']), 'fmt': fmt_of(d), 'tags': tags_for(title, dt)}))
    log(f'unique={len(per_key)} new_candidates={len(new_docs)} existing={len(existing_infos)} errors={len(errors)}')

    # create records
    added = 0
    if new_docs and not args.dry_run:
        records = []
        keys = []
        for k, d in new_docs:
            records.append({
                '文档标题': d['title'],
                '项目': [d['project']],
                '部门': [d['dept']],
                '文档类型': [d['doctype']],
                '格式': [d['fmt']],
                '链接': d['url'] or '',
                '来源': [d.get('mode') or '群聊'],
                '来源群/人': d['chat_name'],
                '最近出现': start.strftime('%Y-%m-%d') + 'T00:00:00+08:00',
                '分享次数': d['occurrences'],
                '状态': ['待确认'],
                '标签': d['tags'],
                '备注': '',
            })
            keys.append(k)
        for i in range(0, len(records), 200):
            chunk = records[i:i+200]
            data = lib.run(['base', '+record-batch-create', '--base-token', BASE_TOKEN, '--table-id', TABLE_ID, '--json', json.dumps({'create_records': chunk}), '--as', 'user'], timeout=180)
            ids = data.get('record_id_list') or []
            for j, rid in enumerate(ids):
                k = keys[i+j]
                d = new_docs[i+j][1]
                record_map[k] = rid
                seen[k] = {'title': d['title'], 'type': d['doctype'], 'first_seen': start.strftime('%Y-%m-%d'), 'last_seen': start.strftime('%Y-%m-%d'), 'occurrences': d['occurrences'], 'record_id': rid}
                added += 1
        save_state(state)

    # summary
    all_infos = new_docs + existing_infos
    types = Counter(d['doctype'] for _, d in all_infos)
    date_label = start.strftime('%m月%d日')
    lines = [f"早上好，昨天（{date_label}）的文档已整理完成。"]
    if args.dry_run:
        lines.append(f"（试运行）共看到 {len(all_infos)} 个去重文档（已剔除财务/数据）。")
    else:
        lines.append(f"共看到 {len(all_infos)} 个去重文档（已剔除财务/数据），新增 {added} 条到表格，{len(existing_infos)} 条已在表格中。")
    if types:
        lines.append("类型：" + "、".join(f"{k} {v}" for k, v in types.most_common(6)))
    titles = [d['title'] for _, d in all_infos]
    if titles:
        lines.append("文档：" + "；".join(titles[:15]) + ("；…" if len(titles) > 15 else ""))
    if errors:
        lines.append(f"有 {len(errors)} 个会话读取失败，已跳过。")
    lines.append(f"[打开知识库]({BASE_URL})")
    msg = "\n".join(lines)
    notify(msg, dry=args.dry_run)
    print(json.dumps({'date': start.strftime('%Y-%m-%d'), 'messages': len(all_msgs), 'unique': len(per_key), 'new': len(new_docs), 'existing': len(existing_infos), 'added': added, 'types': dict(types), 'errors': len(errors)}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log(f'FATAL: {e}')
        time.sleep(10)
        try:
            notify(f'早上好，今天的文档自动整理失败：{str(e)[:300]}。请在飞书回复我重新授权或检查。')
        except Exception as ne:
            log(f'failure notification failed: {ne}')
        raise
