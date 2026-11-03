#!/usr/bin/env python3
"""Scan Feishu chats for documents the user sees: shared links and file attachments.

Uses the raw IM messages API (no reaction-enrichment scope needed).
Read-only. Writes JSON/CSV summary to /tmp/feishu-chat-docs.
"""
import argparse, csv, json, os, re, subprocess, sys, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

LARK = os.environ.get("LARK_CLI", "lark-cli")
OUT = "/tmp/feishu-chat-docs"
TZ = timezone(timedelta(hours=8))
URL_RE = re.compile(r'https?://[^\s<>"\'）)】\]]+')
DOC_PATH_RE = re.compile(r'^/(docx|docs|wiki|sheets|base|slides|mindnotes|file|drive/folder)/([A-Za-z0-9_-]+)')
TYPE_MAP = {"docx": "DOCX", "docs": "DOC", "wiki": "WIKI", "sheets": "SHEET", "base": "BITABLE", "slides": "SLIDES", "mindnotes": "MINDNOTE", "file": "FILE", "drive/folder": "FOLDER"}
DOC_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".csv", ".md", ".txt", ".rtf", ".odt", ".ods", ".odp", ".pages", ".numbers", ".key"}


def log(m): print(m, file=sys.stderr, flush=True)


def iso(ms):
    if not ms: return ""
    try: return datetime.fromtimestamp(int(ms) / 1000, tz=TZ).isoformat()
    except Exception: return ""


def day(ms):
    s = iso(ms)
    return s[:10] if s else ""


def run(args, timeout=120, tries=5, quiet=False):
    cmd = [LARK] + args + ["--format", "json"]
    for attempt in range(tries):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            time.sleep(2 + attempt * 2); continue
        out = p.stdout
        try:
            obj = json.loads(out)
        except Exception:
            # tolerate a warning prefix
            i = out.find("{")
            try: obj = json.loads(out[i:])
            except Exception:
                low = (p.stdout + p.stderr).lower()
                if 'rate' in low or '429' in low or 'frequency' in low:
                    wait = 15 + attempt * 10
                else:
                    wait = 5 + attempt * 5
                if not quiet:
                    log(f"non-JSON rc={p.returncode} stdout={p.stdout[-300:]} stderr={p.stderr[-300:]} sleep={wait}s")
                time.sleep(wait); continue
        if obj.get("ok"): return obj.get("data", {})
        err = obj.get("error") or {}
        msg = f"{err.get('type')}/{err.get('subtype')}: {err.get('message')}"
        if err.get("subtype") == "rate_limit" or err.get("code") == 99991400:
            wait = 5 + attempt * 5
            if not quiet: log(f"rate limit, sleep {wait}s")
            time.sleep(wait); continue
        if err.get("type") in ("authorization", "validation", "permission"):
            raise RuntimeError(msg)
        time.sleep(1 + attempt); last = msg
    raise RuntimeError("request failed")


def parse_doc_url(url):
    """Return (type, token) for a Feishu/Lark doc URL, else None."""
    u = url.strip().rstrip('.,;:!?。，；：！？')
    m = re.match(r'https?://([A-Za-z0-9.-]+\.(?:feishu\.cn|larksuite\.com))/', u)
    if not m: return None
    path = u.split(m.group(1), 1)[1]
    pm = DOC_PATH_RE.match(path)
    if not pm: return None
    kind, token = pm.group(1), pm.group(2)
    return TYPE_MAP.get(kind, kind.upper()), token, u


def walk(obj, urls, files, texts):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("href", "url") and isinstance(v, str):
                urls.append(v)
            if k == "file_key" and isinstance(v, str):
                files.append({"file_key": v, "file_name": obj.get("file_name") or obj.get("name") or ""})
            if k == "text" and isinstance(v, str):
                texts.append(v)
            walk(v, urls, files, texts)
    elif isinstance(obj, list):
        for x in obj: walk(x, urls, files, texts)
    elif isinstance(obj, str):
        for m in URL_RE.finditer(obj):
            urls.append(m.group(0))


def extract(msg):
    """Extract doc links + files + plain text URLs from one message."""
    urls, files, texts = [], [], []
    body = msg.get("body") or {}
    content = body.get("content")
    if isinstance(content, str):
        try:
            obj = json.loads(content)
        except Exception:
            obj = content
    else:
        obj = content
    walk(obj, urls, files, texts)
    # also scan the raw content string in case JSON was malformed
    if isinstance(content, str):
        for m in URL_RE.finditer(content): urls.append(m.group(0))
    docs, file_docs = [], []
    for u in urls:
        d = parse_doc_url(u)
        if d:
            docs.append(d)
    for f in files:
        name = f.get("file_name") or ""
        ext = os.path.splitext(name)[1].lower()
        if f.get("file_key"):
            file_docs.append({**f, "ext": ext, "is_doc": ext in DOC_EXTS})
    return docs, file_docs


def list_chats():
    cache = os.environ.get("FEISHU_CHAT_CACHE", "/tmp/feishu-chats-all.json")
    use_cache = os.path.exists(cache) and (time.time() - os.path.getmtime(cache) < 7 * 86400)
    if use_cache:
        d = json.load(open(cache)).get("data") or {}
    else:
        d = run(["im", "+chat-list", "--types", "p2p,group,topic", "--page-all", "--page-size", "100", "--page-limit", "20", "--as", "user"], quiet=True)
        try:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump({"data": d}, f)
        except Exception:
            pass
    chats = d.get("chats") or d.get("items") or []
    seen, out = set(), []
    for c in chats:
        cid = c.get("chat_id")
        if cid and cid not in seen:
            seen.add(cid); out.append(c)
    return out


def fetch_chat(chat, start_s, end_s, max_pages):
    """Fetch all messages in [start_s,end_s] for a chat; returns (items, pages, error)."""
    cid = chat.get("chat_id")
    items, token, pages = [], None, 0
    try:
        while pages < max_pages:
            params = {
                "container_id": cid, "container_id_type": "chat",
                "start_time": str(start_s), "end_time": str(end_s),
                "sort_type": "ByCreateTimeDesc", "page_size": 50,
            }
            if token: params["page_token"] = token
            d = run(["api", "GET", "/open-apis/im/v1/messages", "--params", json.dumps(params), "--as", "user"], quiet=True)
            page = d.get("items") or []
            items.extend(page); pages += 1
            if not d.get("has_more"): break
            token = d.get("page_token")
            if not token: break
            time.sleep(0.1)
    except Exception as e:
        return items, pages, str(e)
    return items, pages, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit-chats", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-pages", type=int, default=40)
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    now = datetime.now(TZ)
    end = now
    start = now - timedelta(days=args.days)
    start_s, end_s = int(start.timestamp()), int(end.timestamp())
    log(f"window {start.isoformat()} .. {end.isoformat()}")

    chats = list_chats()
    if args.limit_chats: chats = chats[:args.limit_chats]
    log(f"chats: {len(chats)}")

    chat_name = {c.get("chat_id"): c.get("name") or c.get("chat_id") for c in chats}
    chat_mode = {c.get("chat_id"): c.get("chat_mode") or "" for c in chats}

    all_msgs = []
    errors = []
    pages_total = 0
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_chat, c, start_s, end_s, args.max_pages): c for c in chats}
        for fut in as_completed(futs):
            c = futs[fut]
            items, pages, err = fut.result()
            pages_total += pages
            done += 1
            if err:
                errors.append({"chat_id": c.get("chat_id"), "name": c.get("name"), "error": err})
                log(f"[{done}/{len(chats)}] ERROR {c.get('name')}: {err}")
            else:
                all_msgs.extend(items)
                if items:
                    log(f"[{done}/{len(chats)}] {c.get('name')}: {len(items)} msgs, {pages} pages")
                elif done % 100 == 0:
                    log(f"[{done}/{len(chats)}] scanned...")

    log(f"messages: {len(all_msgs)}, pages: {pages_total}, errors: {len(errors)}")

    docs = []  # one per occurrence
    for m in all_msgs:
        cid = m.get("chat_id") or ""
        base = {
            "chat_id": cid, "chat_name": chat_name.get(cid, cid), "chat_mode": chat_mode.get(cid, ""),
            "message_id": m.get("message_id"), "msg_type": m.get("msg_type"),
            "sender_id": (m.get("sender") or {}).get("id") or "",
            "create_time": m.get("create_time"), "create_time_iso": iso(m.get("create_time")),
            "date": day(m.get("create_time")),
        }
        links, files = extract(m)
        for typ, tok, url in links:
            docs.append({**base, "kind": "link", "type": typ, "token": tok, "url": url})
        for f in files:
            if f.get("is_doc"):
                docs.append({**base, "kind": "file", "type": "FILE", "token": f.get("file_key"), "file_name": f.get("file_name"), "ext": f.get("ext"), "url": ""})

    # unique docs
    uniq = {}
    for d in docs:
        k = ("file:" + (d.get("token") or "") + ":" + (d.get("file_name") or "")) if d["kind"] == "file" else ("link:" + d["type"] + ":" + d["token"])
        if not k.split(":", 1)[1]: continue
        u = uniq.get(k)
        if not u:
            uniq[k] = {**d, "occurrences": 1, "chats": {d["chat_id"]: d["chat_name"]}, "dates": {d["date"]}}
        else:
            u["occurrences"] += 1
            u["chats"][d["chat_id"]] = d["chat_name"]
            u["dates"].add(d["date"])
            if not u.get("file_name") and d.get("file_name"): u["file_name"] = d["file_name"]
    unique_docs = list(uniq.values())

    # stats
    by_type = Counter(d["type"] for d in unique_docs)
    by_kind = Counter(d["kind"] for d in unique_docs)
    by_mode = Counter(d["chat_mode"] for d in unique_docs)
    by_chat = Counter()
    for d in unique_docs:
        for cid, name in d["chats"].items(): by_chat[(cid, name, d["chat_mode"])] += 1
    by_day = Counter(d["date"] for d in docs)
    by_sender = Counter(d["sender_id"] for d in unique_docs if d.get("sender_id"))
    # per chat stats for all chats with any doc occurrence
    chat_docs = Counter()
    chat_msgs = Counter(m.get("chat_id") for m in all_msgs)
    for d in docs:
        chat_docs[d["chat_id"]] += 1

    report = {
        "generated_at": now.isoformat(), "window": {"start": start.isoformat(), "end": end.isoformat(), "days": args.days},
        "chats_scanned": len(chats), "chats_with_messages": len(chat_msgs), "messages_scanned": len(all_msgs),
        "pages": pages_total, "errors": errors[:20],
        "total_doc_occurrences": len(docs), "unique_docs": len(unique_docs),
        "unique_by_type": dict(by_type.most_common()), "unique_by_kind": dict(by_kind),
        "unique_by_chat_mode": dict(by_mode),
        "unique_by_day": dict(sorted(Counter(d["date"] for d in unique_docs).items())),
        "occurrences_by_day": dict(sorted(by_day.items())),
        "top_chats": [{"chat_id": cid, "name": name, "mode": mode, "unique": n, "occurrences": chat_docs.get(cid, 0), "messages": chat_msgs.get(cid, 0)} for (cid, name, mode), n in by_chat.most_common(20)],
        "top_senders": [{"sender_id": s, "unique": n} for s, n in by_sender.most_common(20)],
        "chats_with_docs": len(chat_docs),
    }
    with open(os.path.join(OUT, "docs.json"), "w", encoding="utf-8") as f:
        json.dump({"summary": report, "unique_docs": unique_docs, "occurrences": docs}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT, "unique.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "type", "title", "url", "file_name", "ext", "occurrences", "chats", "dates", "sender_id", "first_seen"])
        for d in sorted(unique_docs, key=lambda x: (x.get("create_time") or ""), reverse=True):
            w.writerow([d["kind"], d["type"], d.get("file_name") or "", d.get("url") or "", d.get("file_name") or "", d.get("ext") or "", d["occurrences"], " / ".join(d["chats"].values()), " / ".join(sorted(d["dates"])), d.get("sender_id") or "", d.get("create_time_iso") or ""])
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
