#!/usr/bin/env python3
"""
B站评论爬取 + 点赞投币 — Web 界面后端
运行: python server.py
访问: http://localhost:8765

登录凭证：同目录下读取 credentials.json（格式见 credentials.example.json）；
每次点击「开始」会重新读取 credentials.json，无需重启进程。
评论接口依赖有效登录态：Cookie 过期时接口会返回 0 条评论（视频信息仍正常），需更新 Cookie。
若日志出现 -412：B 站判定请求过快，程序会自动冷却并重试；仍频繁时请拉长定时间隔或减少单次处理量。
"""

import asyncio
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, AsyncGenerator

import httpx
import pandas as pd
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from bilibili_api import comment, user, video, Credential
from bilibili_api.comment import CommentResourceType
from bilibili_api.exceptions import ResponseCodeException

# ══════════════════════════════════════════════════════════
# 路径
# ══════════════════════════════════════════════════════════
BASE_DIR         = Path(__file__).parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
PROCESSED_FILE = BASE_DIR / "processed_users.txt"   # 全局去重（跨视频）
COIN_LOG_FILE  = BASE_DIR / "coin_log.json"
VIDEO_META_FILE= BASE_DIR / "videos_meta.json"       # 已爬视频元数据

def comments_file(bvid: str) -> Path:
    return BASE_DIR / f"comments_{bvid}.xlsx"

# ══════════════════════════════════════════════════════════
# 延迟 & 风控配置
# ══════════════════════════════════════════════════════════
# B 站 -412 = 请求过快/风控，间隔过短易触发，可适当再加大
DELAY_COMMENT_PAGE  = (1.5, 3.0)
DELAY_USER_LOOKUP   = (2.0, 4.0)
DELAY_LIKE_TO_COIN  = (2.5, 5.0)
DELAY_BETWEEN_USERS = (3.0, 6.0)   # 用户间隔

RISK_PAUSE_SEC = 180   # -412 后冷却秒数（会再随机加一点）
MAX_RETRY      = 3

# ══════════════════════════════════════════════════════════
# 可配置参数（/api/config）
# ══════════════════════════════════════════════════════════
_config = {
    "coins_per_person": 1,   # 每人投币枚数：1 或 2
}

# ══════════════════════════════════════════════════════════
# 全局状态
# ══════════════════════════════════════════════════════════
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_task_running      = False
_cancel_requested  = False
_sse_clients: list = []   # 每个 SSE 连接独享一个 Queue，push 广播给所有连接
_task_log_file     = None  # 当前任务的本地日志文件句柄

LOGS_DIR = BASE_DIR / "logs"

_DEFAULT_CREDS = {
    "sessdata": "",
    "bili_jct": "",
    "buvid3": "",
    "dedeuserid": "",
}
_credential_store: dict = {}


def _reload_credential_store():
    global _credential_store
    base = _DEFAULT_CREDS.copy()
    if CREDENTIALS_FILE.exists():
        try:
            data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
            for k in base:
                v = data.get(k)
                if v is not None and str(v).strip() != "":
                    base[k] = str(v).strip()
        except Exception:
            pass
    _credential_store = base


_reload_credential_store()


@app.on_event("startup")
async def startup():
    pass   # _sse_clients 在模块级初始化，无需 startup 动作


# ══════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════

def get_credential() -> Credential:
    missing = [k for k, v in _credential_store.items() if not str(v).strip()]
    if missing:
        raise RuntimeError(
            "缺少登录凭证，请先完善 credentials.json。"
            f" 缺失字段: {', '.join(missing)}"
        )
    return Credential(
        sessdata=_credential_store["sessdata"],
        bili_jct=_credential_store["bili_jct"],
        buvid3=_credential_store["buvid3"],
        dedeuserid=_credential_store["dedeuserid"],
    )

def extract_bvid(raw: str) -> str:
    m = re.search(r"BV[a-zA-Z0-9]+", raw)
    if m:
        return m.group(0)
    raise ValueError(f"无法解析 BV ID: {raw}")

async def rand_sleep(range_: tuple):
    """异步等待，不阻塞事件循环，SSE 消息照常实时推送"""
    await asyncio.sleep(random.uniform(*range_))

# ── 去重（全局，跨视频）──────────────────────────────────

def load_processed_users() -> set:
    if PROCESSED_FILE.exists():
        return {l.strip() for l in PROCESSED_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}
    return set()

def mark_user_processed(uid: str):
    with open(PROCESSED_FILE, "a", encoding="utf-8") as f:
        f.write(uid + "\n")

# ── 每日投币计数（文件持久化）────────────────────────────

def _load_coin_log() -> dict:
    if COIN_LOG_FILE.exists():
        try:
            return json.loads(COIN_LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_coin_log(data: dict):
    COIN_LOG_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

def get_coins_given_today() -> int:
    return _load_coin_log().get(datetime.now().strftime("%Y-%m-%d"), 0)

def add_coin_today(n: int = 1) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    data  = _load_coin_log()
    data[today] = data.get(today, 0) + n
    _save_coin_log(data)
    return data[today]

# ── 评论表格（每视频独立文件）───────────────────────────

_COLUMNS = ["uid", "用户名", "头像URL", "评论内容", "爬取时间", "状态"]

def load_comments(bvid: str) -> pd.DataFrame:
    f = comments_file(bvid)
    if f.exists():
        df = pd.read_excel(f, dtype=str)
        for col in _COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[_COLUMNS]
    return pd.DataFrame(columns=_COLUMNS)

def save_comments(df: pd.DataFrame, bvid: str):
    df.to_excel(comments_file(bvid), index=False)

def update_comment_status(bvid: str, uid: str, status: str):
    """更新单条评论的状态并持久化到 Excel"""
    f = comments_file(bvid)
    if not f.exists():
        return
    df = pd.read_excel(f, dtype=str)
    df.loc[df["uid"] == uid, "状态"] = status
    df.to_excel(f, index=False)

# ── 视频元数据（记录已爬视频列表）──────────────────────

def load_video_meta() -> dict:
    if VIDEO_META_FILE.exists():
        try:
            return json.loads(VIDEO_META_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_video_meta(bvid: str, title: str, cover: str):
    meta = load_video_meta()
    meta[bvid] = {
        "bvid":       bvid,
        "title":      title,
        "cover":      cover,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    VIDEO_META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

# ── SSE 推送 ─────────────────────────────────────────────

async def push(type_: str, **kwargs):
    """广播给所有当前 SSE 连接，log 类型同步写本地日志文件"""
    msg = {"type": type_, **kwargs}
    for q in list(_sse_clients):
        await q.put(msg)
    if type_ == "log" and _task_log_file is not None:
        ts   = datetime.now().strftime("%H:%M:%S")
        text = kwargs.get("msg", "").strip()
        try:
            _task_log_file.write(f"[{ts}] {text}\n")
            _task_log_file.flush()
        except Exception:
            pass

async def push_risk(msg: str):
    await push("log", level="error", msg=f"🚨 [风控] {msg}")
    await push("risk_alert", msg=msg)


# ══════════════════════════════════════════════════════════
# 核心业务逻辑
# ══════════════════════════════════════════════════════════

async def fetch_all_comments(bvid: str, credential: Credential) -> list:
    await push("log", level="info", msg=f"🔍 获取视频信息: {bvid}")
    v    = video.Video(bvid=bvid, credential=credential)
    info = await v.get_info()
    aid  = info["aid"]
    title = info.get("title", bvid)
    cover = info.get("pic", "")

    await push("log", level="info", msg=f"📺 《{title}》  (aid={aid})")
    await push("video_info", title=title, bvid=bvid, cover=cover)
    save_video_meta(bvid, title, cover)

    all_comments = []
    page  = 1
    total = 0   # 在循环外初始化，loop 后用于最终推满
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    while True:
        result = None
        for attempt in range(MAX_RETRY + 2):
            try:
                result = await comment.get_comments(
                    oid=aid,
                    type_=CommentResourceType.VIDEO,
                    page_index=page,
                    credential=credential,
                )
                break
            except ResponseCodeException as e:
                if e.code == -412:
                    await push_risk(
                        f"评论第 {page} 页触发风控 (-412：请求过快)，冷却约 {RISK_PAUSE_SEC}s 后重试 "
                        f"({attempt + 1}/{MAX_RETRY + 2})"
                    )
                    await rand_sleep((RISK_PAUSE_SEC, RISK_PAUSE_SEC + 120))
                else:
                    await push("log", level="warn", msg=f"⚠️ 第 {page} 页评论失败: code={e.code} {e}")
                    break
            except Exception as e:
                await push("log", level="warn", msg=f"⚠️ 第 {page} 页评论获取失败: {e}")
                break
        if result is None:
            break

        replies   = result.get("replies") or []
        page_info = result.get("page", {})
        total     = page_info.get("count", 0)
        size      = page_info.get("size", 20)

        if not replies:
            if page == 1:
                if total == 0:
                    await push(
                        "log",
                        level="warn",
                        msg="⚠️ 评论接口返回 0 条。若网页上能看到评论，通常是 **登录 Cookie 失效或未生效**："
                            "请检查 credentials.json 是否为最新，保存后再点一次「开始」（已自动重读文件）。"
                            "若作者关闭评论或确实无人留言，则正常。",
                    )
                else:
                    await push(
                        "log",
                        level="warn",
                        msg=f"⚠️ 第 1 页无评论数据但接口称共 {total} 条，可能接口异常，请稍后重试或更新 Cookie。",
                    )
            break

        for r in replies:
            member  = r.get("member", {})
            content = r.get("content", {})
            uid = str(member.get("mid", ""))
            if not uid:
                continue
            all_comments.append({
                "uid":     uid,
                "用户名":  member.get("uname", ""),
                "头像URL": member.get("avatar", ""),
                "评论内容": content.get("message", ""),
                "爬取时间": now_s,
                "状态":    "待处理",
            })

        fetched = page * size
        await push("log", level="info",
                   msg=f"  第 {page} 页: {len(replies)} 条  (累计 {min(fetched, total)}/{total})")
        await push("progress", fetched=min(fetched, total), total=total)

        if fetched >= total:
            break
        page += 1
        await rand_sleep(DELAY_COMMENT_PAGE)

    # 确保进度条达到 100%
    if total > 0:
        await push("progress", fetched=total, total=total)

    await push("log", level="success", msg=f"✅ 共爬取 {len(all_comments)} 条一级评论")
    return all_comments


async def get_user_first_video(uid: str, credential: Credential) -> Optional[str]:
    for attempt in range(MAX_RETRY + 1):
        try:
            u      = user.User(uid=int(uid), credential=credential)
            result = await u.get_videos(pn=1, ps=1)
            vlist  = result.get("list", {}).get("vlist", [])
            return vlist[0].get("bvid") if vlist else None
        except ResponseCodeException as e:
            if e.code == -412:
                await push_risk(f"查询用户 {uid} 主页被拦截，暂停 {RISK_PAUSE_SEC}s")
                await rand_sleep((RISK_PAUSE_SEC, RISK_PAUSE_SEC + 30))
            elif attempt < MAX_RETRY:
                await rand_sleep((5, 10))
            else:
                await push("log", level="warn", msg=f"  ⚠️ 获取用户 {uid} 视频失败: code={e.code}")
        except Exception as e:
            await push("log", level="warn", msg=f"  ⚠️ 获取用户 {uid} 视频异常: {e}")
            break
    return None


async def like_and_coin(bvid: str, credential: Credential) -> bool:
    """点赞 + 投币（无单日上限，仅受账号硬币余额与 B 站接口限制）。"""
    v = video.Video(bvid=bvid, credential=credential)
    coins_to_give = _config["coins_per_person"]

    # ── 点赞 ──────────────────────────────────────────────
    for attempt in range(MAX_RETRY + 1):
        try:
            await v.like(True)
            await push("log", level="success", msg="  👍 点赞成功")
            break
        except ResponseCodeException as e:
            if e.code == 22001:
                await push("log", level="info", msg="  👍 已点过赞，跳过")
                break
            elif e.code == -412:
                await push_risk(f"点赞被拦截，暂停 {RISK_PAUSE_SEC}s")
                await rand_sleep((RISK_PAUSE_SEC, RISK_PAUSE_SEC + 60))
            elif e.code == -101:
                await push("log", level="error", msg="  ❌ 未登录，请检查 Cookie")
                return False
            elif attempt < MAX_RETRY:
                await rand_sleep((5, 15))
            else:
                await push("log", level="warn", msg=f"  ⚠️ 点赞失败: code={e.code}")
        except Exception as e:
            await push("log", level="warn", msg=f"  ⚠️ 点赞异常: {e}")
            break

    await rand_sleep(DELAY_LIKE_TO_COIN)

    # ── 投币 ──────────────────────────────────────────────
    for attempt in range(MAX_RETRY + 1):
        try:
            await v.pay_coin(num=coins_to_give, like=True)
            used = add_coin_today(coins_to_give)
            await push("log", level="success",
                       msg=f"  🪙 投 {coins_to_give} 枚成功（今日累计 {used} 枚）")
            await push("coin_update", used=used)
            break
        except ResponseCodeException as e:
            if e.code in (34005, 34003):
                await push("log", level="info", msg="  🪙 已投过币，跳过")
                break
            elif e.code == 34002:
                await push("log", level="warn", msg="  🪙 硬币不足，跳过")
                break
            elif e.code == -412:
                await push_risk(f"投币被拦截，暂停 {RISK_PAUSE_SEC}s")
                await rand_sleep((RISK_PAUSE_SEC, RISK_PAUSE_SEC + 60))
            elif e.code == -101:
                await push("log", level="error", msg="  ❌ 未登录，请检查 Cookie")
                break
            elif attempt < MAX_RETRY:
                await rand_sleep((5, 15))
            else:
                await push("log", level="warn", msg=f"  ⚠️ 投币失败: code={e.code}")
        except Exception as e:
            await push("log", level="warn", msg=f"  ⚠️ 投币异常: {e}")
            break

    return True


async def run_task(video_input: str):
    global _task_running, _cancel_requested
    _task_running     = True
    _cancel_requested = False
    _reload_credential_store()   # 每次任务前重读 credentials.json（改完不用重启）
    credential    = get_credential()
    stats = {"total": 0, "new_comments": 0, "processed": 0, "skipped": 0}

    try:
        await _run_task_inner(video_input, credential, stats)
    except Exception as e:
        await push("log", level="error", msg=f"❌ 任务异常: {e}")
        await push("done", stats=stats)
    finally:
        _task_running     = False
        _cancel_requested = False


async def _run_task_inner(video_input: str, credential, stats: dict):
    global _task_log_file
    try:
        bvid = extract_bvid(video_input)
    except ValueError as e:
        await push("log", level="error", msg=f"❌ {e}")
        await push("done", stats=stats)
        return

    # 打开本地日志文件（追加模式）
    LOGS_DIR.mkdir(exist_ok=True)
    log_path = LOGS_DIR / f"{bvid}.txt"
    run_ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _task_log_file = open(log_path, "a", encoding="utf-8")
        _task_log_file.write(f"\n{'═' * 44}\n{run_ts}  任务开始\n{'═' * 44}\n")
        _task_log_file.flush()
    except Exception:
        _task_log_file = None

    try:
        await _do_task(bvid, credential, stats)
    finally:
        if _task_log_file is not None:
            end_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                _task_log_file.write(f"{'═' * 44}\n{end_ts}  任务结束\n")
                _task_log_file.close()
            except Exception:
                pass
            _task_log_file = None


async def _do_task(bvid: str, credential, stats: dict):
    await push("log", level="info", msg="═" * 42)
    await push("log", level="info", msg=f"🎬 视频: {bvid}")
    await push("log", level="info",
               msg=f"🪙 今日已累计投币: {get_coins_given_today()} 枚（每人 {_config['coins_per_person']} 枚）")
    await push("log", level="info", msg="═" * 42)
    await push("coin_update", used=get_coins_given_today())

    # ① 爬评论
    fetched = await fetch_all_comments(bvid, credential)
    stats["total"] = len(fetched)

    # ② 合并去重（仅针对本视频）
    existing_df   = load_comments(bvid)
    existing_uids = set(existing_df["uid"].tolist()) if not existing_df.empty else set()
    new_records   = [c for c in fetched if c["uid"] not in existing_uids]
    stats["new_comments"] = len(new_records)

    if new_records:
        new_df      = pd.DataFrame(new_records, columns=_COLUMNS)
        combined_df = pd.concat([existing_df, new_df], ignore_index=True)
        combined_df = combined_df.drop_duplicates(subset=["uid"], keep="first")
        save_comments(combined_df, bvid)
        await push("log", level="info",
                   msg=f"📊 新增 {len(new_records)} 人（本视频共 {len(combined_df)} 条）")
        await push("new_rows", rows=new_records, bvid=bvid)
        await push("video_count", bvid=bvid, count=len(combined_df))
    else:
        await push("log", level="info",
                   msg=f"📊 无新增评论者（已有 {len(existing_df)} 条）")

    # ③ 过滤未处理用户（全局去重，跨视频）
    processed_users = load_processed_users()
    new_uids         = [c["uid"] for c in new_records if c["uid"] not in processed_users]
    already_done_uids= [c["uid"] for c in new_records if c["uid"] in processed_users]

    # 把跨视频已处理过的人状态写回 Excel
    if already_done_uids:
        for uid in already_done_uids:
            update_comment_status(bvid, uid, "↩️ 已处理过")
            await push("update_row", uid=uid, bvid=bvid, status="↩️ 已处理过")
        await push("log", level="info",
                   msg=f"↩️ {len(already_done_uids)} 人曾在其他视频处理过，跳过")

    if not new_uids:
        await push("log", level="success", msg="✅ 没有需要处理的新用户")
        await push("done", stats=stats)
        return

    total_new = len(new_uids)

    await push("log", level="info", msg=f"🚀 处理 {total_new} 个新评论者...")

    for i, uid in enumerate(new_uids, 1):
        if _cancel_requested:
            await push("log", level="warn", msg="🛑 任务已手动终止")
            await push("done", stats=stats)
            return

        uname = next((c["用户名"] for c in new_records if c["uid"] == uid), uid)
        await push("log", level="info", msg=f"\n[{i}/{total_new}] {uname} ({uid})")
        await push("user_progress", current=i, total=total_new, uid=uid, uname=uname)

        await rand_sleep(DELAY_USER_LOOKUP)
        bvid_target = await get_user_first_video(uid, credential)

        if bvid_target:
            await push("log", level="info", msg=f"  📹 {bvid_target}")
            await like_and_coin(bvid_target, credential)
            stats["processed"] += 1
            status = "✅ 已完成"
        else:
            await push("log", level="warn", msg="  ℹ️ 无投稿视频，跳过")
            stats["skipped"] += 1
            status = "⏭️ 无视频"

        update_comment_status(bvid, uid, status)          # 写回 Excel
        await push("update_row", uid=uid, bvid=bvid, status=status)

        mark_user_processed(uid)

        if i < total_new:
            t = round(random.uniform(*DELAY_BETWEEN_USERS), 1)
            await push("log", level="info", msg=f"  ⏳ 等待 {t}s…")
            await rand_sleep((t, t))

    await push("log", level="success",
               msg=f"\n🎉 完成！点赞投币 {stats['processed']} 人，跳过 {stats['skipped']} 人")
    await push("done", stats=stats)


# ══════════════════════════════════════════════════════════
# API 路由
# ══════════════════════════════════════════════════════════

@app.post("/api/cancel")
async def api_cancel():
    global _cancel_requested
    if not _task_running:
        return JSONResponse({"error": "当前没有运行中的任务"}, status_code=400)
    _cancel_requested = True
    return {"status": "cancel_requested"}


@app.post("/api/start")
async def api_start(body: dict):
    global _task_running
    if _task_running:
        return JSONResponse({"error": "任务正在运行中，请等待完成"}, status_code=409)
    url = body.get("url", "").strip()
    if not url:
        return JSONResponse({"error": "请输入视频链接或 BV 号"}, status_code=400)
    asyncio.create_task(run_task(url))
    return {"status": "started"}


@app.get("/api/stream")
async def api_stream():
    q: asyncio.Queue = asyncio.Queue()
    _sse_clients.append(q)

    async def event_gen() -> AsyncGenerator[str, None]:
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        except (GeneratorExit, asyncio.CancelledError):
            pass
        finally:
            if q in _sse_clients:
                _sse_clients.remove(q)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.delete("/api/videos/{bvid}")
async def api_delete_video(bvid: str):
    """删除视频记录（元数据 + 评论表格）"""
    meta = load_video_meta()
    if bvid not in meta:
        return JSONResponse({"error": "视频不存在"}, status_code=404)
    del meta[bvid]
    VIDEO_META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    f = comments_file(bvid)
    if f.exists():
        f.unlink()
    return {"ok": True, "bvid": bvid}


@app.get("/api/videos")
async def api_videos():
    """返回所有已爬取视频的元数据 + 评论数"""
    meta = load_video_meta()
    result = []
    for bvid, info in meta.items():
        f = comments_file(bvid)
        count = 0
        if f.exists():
            try:
                count = len(pd.read_excel(f, dtype=str))
            except Exception:
                pass
        result.append({**info, "count": count})
    # 按更新时间倒序
    result.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return {"videos": result}


@app.get("/api/comments")
async def api_comments(
    bvid:     str = Query(None),
    page:     int = Query(1, ge=1),
    per_page: int = Query(20, ge=5, le=100),
    q:        str = Query(""),
):
    """分页获取评论，支持按视频和关键词过滤"""
    if not bvid:
        # 返回最新视频
        meta = load_video_meta()
        if not meta:
            return {"rows": [], "total": 0, "pages": 0, "page": 1}
        bvid = next(iter(meta))

    df = load_comments(bvid)
    if df.empty:
        return {"rows": [], "total": 0, "pages": 0, "page": 1}

    df = df.fillna("")

    if q:
        mask = (
            df["用户名"].str.contains(q, case=False, na=False) |
            df["uid"].str.contains(q, na=False) |
            df["评论内容"].str.contains(q, case=False, na=False)
        )
        df = df[mask]

    total = len(df)
    pages = max(1, (total + per_page - 1) // per_page)
    page  = min(page, pages)
    start = (page - 1) * per_page
    rows  = df.iloc[start : start + per_page].to_dict(orient="records")

    return {"rows": rows, "total": total, "pages": pages, "page": page, "per_page": per_page}


@app.get("/api/stats")
async def api_stats():
    meta      = load_video_meta()
    processed = load_processed_users()
    total_comments = sum(
        len(pd.read_excel(comments_file(bvid), dtype=str))
        for bvid in meta
        if comments_file(bvid).exists()
    ) if meta else 0
    return {
        "total_comments":  total_comments,
        "processed_users": len(processed),
        "running":         _task_running,
        "coins_used":      get_coins_given_today(),
        "video_count":     len(meta),
    }


@app.post("/api/update_cookie")
async def api_update_cookie(body: dict):
    """接收完整 Cookie 字符串，解析关键字段并保存到 credentials.json，立即生效。"""
    raw = body.get("cookie", "").strip()
    if not raw:
        return JSONResponse({"ok": False, "error": "Cookie 不能为空"}, status_code=400)

    def _pick(name: str) -> str:
        m = re.search(r'(?:^|;)\s*' + re.escape(name) + r'=([^;]+)', raw)
        return m.group(1).strip() if m else ""

    sessdata   = _pick("SESSDATA")
    bili_jct   = _pick("bili_jct")
    buvid3     = _pick("buvid3")
    dedeuserid = _pick("DedeUserID")

    missing = [n for n, v in [("SESSDATA", sessdata), ("bili_jct", bili_jct),
                               ("buvid3", buvid3), ("DedeUserID", dedeuserid)] if not v]
    if missing:
        return JSONResponse({"ok": False, "error": f"未找到字段: {', '.join(missing)}"}, status_code=400)

    data = {"sessdata": sessdata, "bili_jct": bili_jct,
            "buvid3": buvid3, "dedeuserid": dedeuserid}
    CREDENTIALS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _reload_credential_store()
    return {"ok": True, "dedeuserid": dedeuserid}


@app.get("/api/config")
async def get_config():
    return _config

@app.post("/api/config")
async def set_config(body: dict):
    if "coins_per_person" in body:
        v = int(body["coins_per_person"])
        _config["coins_per_person"] = max(1, min(2, v))
    return _config


@app.post("/api/manual_action")
async def api_manual_action(body: dict):
    """手动对某条评论的用户执行点赞投币。可在主任务运行期间并行调用。"""
    uid         = str(body.get("uid", "")).strip()
    source_bvid = str(body.get("source_bvid", "")).strip()
    if not uid:
        return JSONResponse({"ok": False, "error": "缺少 uid"}, status_code=400)

    _reload_credential_store()
    credential = get_credential()

    await push("log", level="info", msg=f"\n🖱️ 手动操作: uid={uid}")

    bvid_target = await get_user_first_video(uid, credential)
    if not bvid_target:
        status = "⏭️ 无视频"
        if source_bvid:
            update_comment_status(source_bvid, uid, status)
        await push("update_row", uid=uid, bvid=source_bvid, status=status)
        return {"ok": True, "status": status, "msg": "该用户无投稿视频"}

    await push("log", level="info", msg=f"  📹 {bvid_target}")
    ok = await like_and_coin(bvid_target, credential)
    status = "✅ 已完成" if ok else "❌ 操作失败"
    if source_bvid:
        update_comment_status(source_bvid, uid, status)
    mark_user_processed(uid)
    await push("update_row", uid=uid, bvid=source_bvid, status=status)
    used = get_coins_given_today()
    await push("coin_update", used=used)
    return {"ok": ok, "status": status, "bvid_target": bvid_target}


@app.get("/api/avatar")
async def proxy_avatar(url: str = Query(...)):
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url, headers={
                "Referer": "https://www.bilibili.com/",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            })
        return Response(content=resp.content,
                        media_type=resp.headers.get("content-type", "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        return Response(status_code=404)


@app.get("/", response_class=HTMLResponse)
async def index():
    p = Path(__file__).parent / "templates" / "index.html"
    if not p.exists():
        return HTMLResponse(
            "<h1>缺少 templates/index.html</h1><p>请从项目仓库恢复该文件。</p>",
            status_code=503,
        )
    return p.read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn
    print("🚀 启动 Web 界面: http://localhost:8765")
    if CREDENTIALS_FILE.exists():
        print("   凭证: credentials.json（每次开始任务会自动重读）")
    else:
        print("   凭证: 内置 _DEFAULT_CREDS（可新增 credentials.json 覆盖）")
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="warning")
