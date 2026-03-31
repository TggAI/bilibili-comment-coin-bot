import asyncio
import json
from pathlib import Path
from bilibili_api import video, Credential

CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"

# 目标视频 BV号（用户85644194主页第一个视频）
BVID = "BV15nNgz7EPV"

async def main():
    data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    credential = Credential(
        sessdata=str(data.get("sessdata", "")).strip(),
        bili_jct=str(data.get("bili_jct", "")).strip(),
        buvid3=str(data.get("buvid3", "")).strip(),
        dedeuserid=str(data.get("dedeuserid", "")).strip(),
    )

    v = video.Video(bvid=BVID, credential=credential)

    # 1. 点赞
    print("正在点赞...")
    try:
        like_result = await v.like(True)
        print(f"点赞结果: {like_result}")
    except Exception as e:
        print(f"点赞失败: {e}")

    # 2. 投币（投1枚，同时点赞）
    print("正在投币...")
    try:
        coin_result = await v.pay_coin(1, like=True)
        print(f"投币结果: {coin_result}")
    except Exception as e:
        print(f"投币失败: {e}")

    print("操作完成！")

asyncio.run(main())
