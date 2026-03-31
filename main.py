#!/usr/bin/env python3
"""
B站评论爬取 + 自动点赞投币系统

功能：
1. 爬取指定视频的所有一级评论（支持分页），保存用户头像、UID、评论内容到 Excel
2. 对每个评论者：访问其主页，若有视频则对第一个视频点赞 + 投币
3. 去重：记录已处理用户，再次运行只处理新增评论者
4. 防封：操作间随机延迟

用法：
    python main.py <视频URL或BV号>
    python main.py BV1ABcsztEcY
    python main.py https://www.bilibili.com/video/BV1ABcsztEcY
"""

import asyncio
import json
import os
import re
import sys
import time
import random
from datetime import datetime
from pathlib import Path

from typing import Optional

import pandas as pd
from bilibili_api import comment, user, video, Credential
from bilibili_api.comment import CommentResourceType

# ══════════════════════════════════════════════════════════
# 文件路径（与脚本同目录）
# ══════════════════════════════════════════════════════════
BASE_DIR        = Path(__file__).parent
COMMENTS_FILE   = BASE_DIR / "comments.xlsx"
PROCESSED_FILE  = BASE_DIR / "processed_users.txt"
CREDENTIALS_FILE = BASE_DIR / "credentials.json"

# ══════════════════════════════════════════════════════════
# 延迟配置（单位：秒）
# ══════════════════════════════════════════════════════════
DELAY_COMMENT_PAGE  = (1.0, 2.0)   # 每页评论请求间隔
DELAY_USER_ACTION   = (2.0, 4.0)   # 用户操作（点赞/投币）间隔
DELAY_BETWEEN_USERS = (3.0, 6.0)   # 不同用户之间间隔


# ──────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────

def get_credential() -> Credential:
    # 优先读取 credentials.json，不存在时回退到环境变量
    creds = {"sessdata": "", "bili_jct": "", "buvid3": "", "dedeuserid": ""}
    if CREDENTIALS_FILE.exists():
        data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
        for key in creds:
            value = data.get(key)
            if value is not None:
                creds[key] = str(value).strip()
    else:
        creds["sessdata"] = os.getenv("BILI_SESSDATA", "").strip()
        creds["bili_jct"] = os.getenv("BILI_JCT", "").strip()
        creds["buvid3"] = os.getenv("BILI_BUVID3", "").strip()
        creds["dedeuserid"] = os.getenv("BILI_DEDEUSERID", "").strip()

    missing = [k for k, v in creds.items() if not v]
    if missing:
        raise RuntimeError(
            "缺少登录凭证，请填写 credentials.json（推荐）或环境变量 "
            f"BILI_SESSDATA/BILI_JCT/BILI_BUVID3/BILI_DEDEUSERID。缺失字段: {', '.join(missing)}"
        )

    return Credential(
        sessdata=creds["sessdata"],
        bili_jct=creds["bili_jct"],
        buvid3=creds["buvid3"],
        dedeuserid=creds["dedeuserid"],
    )


def extract_bvid(raw: str) -> str:
    """从 URL 或纯 BV 号中提取 BV ID"""
    m = re.search(r"BV[a-zA-Z0-9]+", raw)
    if m:
        return m.group(0)
    raise ValueError(f"无法从 '{raw}' 中解析 BV ID，请检查输入格式")


def rand_sleep(range_: tuple):
    t = random.uniform(*range_)
    time.sleep(t)


# ──────────────────────────────────────────────────────────
# 去重：已处理用户管理
# ──────────────────────────────────────────────────────────

def load_processed_users() -> set:
    """读取已处理的用户 UID 集合"""
    if PROCESSED_FILE.exists():
        return {line.strip() for line in PROCESSED_FILE.read_text(encoding="utf-8").splitlines() if line.strip()}
    return set()


def mark_user_processed(uid: str):
    """将 UID 追加到已处理文件"""
    with open(PROCESSED_FILE, "a", encoding="utf-8") as f:
        f.write(uid + "\n")


# ──────────────────────────────────────────────────────────
# Excel 评论表格管理
# ──────────────────────────────────────────────────────────

_COLUMNS = ["uid", "用户名", "头像URL", "评论内容", "爬取时间"]


def load_existing_comments() -> pd.DataFrame:
    if COMMENTS_FILE.exists():
        df = pd.read_excel(COMMENTS_FILE, dtype=str)
        # 确保列完整
        for col in _COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[_COLUMNS]
    return pd.DataFrame(columns=_COLUMNS)


def save_comments(df: pd.DataFrame):
    df.to_excel(COMMENTS_FILE, index=False)
    print(f"  💾 已保存 {len(df)} 条评论 → {COMMENTS_FILE.name}")


# ──────────────────────────────────────────────────────────
# 爬取评论
# ──────────────────────────────────────────────────────────

async def fetch_all_comments(bvid: str, credential: Credential) -> list[dict]:
    """爬取视频所有一级评论（自动翻页）"""
    print(f"🔍 正在获取视频信息: {bvid} ...")

    # 通过 bvid 获取 aid（评论接口需要 aid/oid）
    v = video.Video(bvid=bvid, credential=credential)
    info = await v.get_info()
    aid = info["aid"]
    title = info.get("title", bvid)
    print(f"  标题: {title}  (aid={aid})")

    all_comments: list[dict] = []
    page = 1
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    while True:
        try:
            result = await comment.get_comments(
                oid=aid,
                type_=CommentResourceType.VIDEO,
                page_index=page,
                credential=credential,
            )
        except Exception as e:
            print(f"  ⚠️ 第 {page} 页评论获取失败: {e}")
            break

        replies = result.get("replies") or []
        if not replies:
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
                "爬取时间": now_str,
            })

        page_info = result.get("page", {})
        total     = page_info.get("count", 0)
        size      = page_info.get("size", 20)
        fetched   = page * size

        print(f"  第 {page} 页: {len(replies)} 条评论  (累计 {min(fetched, total)}/{total})")

        if fetched >= total:
            break

        page += 1
        rand_sleep(DELAY_COMMENT_PAGE)

    print(f"  ✅ 共爬取 {len(all_comments)} 条一级评论")
    return all_comments


# ──────────────────────────────────────────────────────────
# 用户操作：获取视频 → 点赞投币
# ──────────────────────────────────────────────────────────

async def get_user_first_video(uid: str, credential: Credential) -> Optional[str]:
    """获取用户最新发布的第一个视频 BV ID，无视频返回 None"""
    try:
        u = user.User(uid=int(uid), credential=credential)
        result = await u.get_videos(pn=1, ps=1)
        vlist = result.get("list", {}).get("vlist", [])
        if vlist:
            return vlist[0].get("bvid")
    except Exception as e:
        print(f"    ⚠️ 获取用户 {uid} 视频失败: {e}")
    return None


async def like_and_coin(bvid: str, credential: Credential):
    """对指定视频执行点赞 + 投1币"""
    v = video.Video(bvid=bvid, credential=credential)

    # 点赞
    try:
        await v.like(True)
        print(f"    👍 点赞成功")
    except Exception as e:
        print(f"    ⚠️ 点赞失败: {e}")

    rand_sleep(DELAY_USER_ACTION)

    # 投币（同时点赞，避免重复计入）
    try:
        await v.pay_coin(num=1, like=True)
        print(f"    🪙 投币成功")
    except Exception as e:
        print(f"    ⚠️ 投币失败: {e}")


async def process_new_users(new_uids: list[str], credential: Credential):
    """对所有新用户依次执行检视频 → 点赞投币，完成后标记"""
    total = len(new_uids)
    print(f"\n🚀 开始处理 {total} 个新评论者...")

    for i, uid in enumerate(new_uids, 1):
        print(f"\n[{i}/{total}] UID: {uid}")

        bvid = await get_user_first_video(uid, credential)
        if bvid:
            print(f"    📹 找到视频: {bvid}")
            await like_and_coin(bvid, credential)
        else:
            print(f"    ℹ️ 该用户暂无投稿视频，跳过")

        # 无论是否有视频，都标记为已处理（避免重复访问）
        mark_user_processed(uid)

        if i < total:
            rand_sleep(DELAY_BETWEEN_USERS)

    print(f"\n✅ 本轮处理完成，共处理 {total} 个新用户")


# ──────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────

async def main(video_input: str):
    bvid       = extract_bvid(video_input)
    credential = get_credential()

    print("=" * 55)
    print(f"🎬  目标视频: {bvid}")
    print("=" * 55)

    # ① 爬取评论
    fetched_comments = await fetch_all_comments(bvid, credential)

    if not fetched_comments:
        print("⚠️ 未获取到任何评论，程序退出")
        return

    # ② 加载已有表格，合并 + 去重
    existing_df  = load_existing_comments()
    existing_uids = set(existing_df["uid"].tolist()) if not existing_df.empty else set()

    new_records = [c for c in fetched_comments if c["uid"] not in existing_uids]

    if new_records:
        new_df      = pd.DataFrame(new_records, columns=_COLUMNS)
        combined_df = pd.concat([existing_df, new_df], ignore_index=True)
        combined_df = combined_df.drop_duplicates(subset=["uid"], keep="first")
        save_comments(combined_df)
        print(f"  📊 新增评论者 {len(new_records)} 人（表格共 {len(combined_df)} 行）")
    else:
        print(f"  📊 无新增评论者（表格共 {len(existing_df)} 行）")

    # ③ 过滤出未处理的新用户
    processed_users = load_processed_users()
    new_uids = [c["uid"] for c in new_records if c["uid"] not in processed_users]

    if not new_uids:
        print("\n✅ 没有需要点赞投币的新用户，程序退出")
        return

    # ④ 依次处理新用户
    await process_new_users(new_uids, credential)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print("示例:")
        print("  python main.py BV1ABcsztEcY")
        print("  python main.py https://www.bilibili.com/video/BV1ABcsztEcY")
        sys.exit(1)

    asyncio.run(main(sys.argv[1]))
