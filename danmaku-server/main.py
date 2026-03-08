"""Bilibili 弹幕/字幕 → ASS 格式转换 API 服务"""


import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Optional

import httpx
from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse



app = FastAPI(
    title="Bilibili Danmaku API",
    description="将B站弹幕/字幕转换为 ASS 字幕格式",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


http_client: httpx.AsyncClient | None = None

BILIBILI_DANMAKU_URL = "https://comment.bilibili.com/{cid}.xml"
BILIBILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://www.bilibili.com",
}



_rate_limit: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_MAX = 60  # requests
RATE_LIMIT_WINDOW = 60  # seconds


def _check_rate_limit(ip: str) -> bool:
    """Return True if the request should be allowed."""
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW

    _rate_limit[ip] = [t for t in _rate_limit[ip] if t > window_start]
    if len(_rate_limit[ip]) >= RATE_LIMIT_MAX:
        return False
    _rate_limit[ip].append(now)
    return True





@app.on_event("startup")
async def _startup():
    global http_client
    http_client = httpx.AsyncClient(
        headers=HEADERS,
        timeout=httpx.Timeout(15.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )


@app.on_event("shutdown")
async def _shutdown():
    if http_client:
        await http_client.aclose()





@app.get("/health")
@app.get("/ping")
async def health():
    return {"status": "ok"}





@app.get("/subtitle")
async def subtitle(
    request: Request,
    cid: Optional[str] = Query(None, description="B站视频 cid"),
    bvid: Optional[str] = Query(None, description="B站视频 BV 号"),
    url: Optional[str] = Query(None, description="B站 JSON 字幕 URL"),
    font: str = Query("微软雅黑", description="字体名称"),
    font_size: float = Query(30.0, description="字号"),
    alpha: float = Query(0.8, description="不透明度 0.0-1.0"),
    display_area: float = Query(0.8, description="弹幕显示区域 0.0-1.0"),
    duration_marquee: float = Query(15.0, description="滚动弹幕持续时间(秒)"),
    duration_still: float = Query(15.0, description="固定弹幕持续时间(秒)"),
):


    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        return JSONResponse(
            status_code=429,
            content={"error": "请求过于频繁，请稍后再试"},
        )


    if url:
        return await _handle_subtitle_proxy(url, font, font_size)


    if cid:
        return await _handle_danmaku(
            cid, font, font_size, alpha, display_area, duration_marquee, duration_still
        )


    if bvid:
        resolved_cid = await _resolve_cid(bvid)
        if not resolved_cid:
            return PlainTextResponse("无法从 bvid 解析 cid", status_code=400)
        return await _handle_danmaku(
            resolved_cid, font, font_size, alpha, display_area,
            duration_marquee, duration_still,
        )

    return PlainTextResponse("请提供 cid、bvid 或 url 参数", status_code=400)





async def _resolve_cid(bvid: str) -> Optional[str]:
    """Resolve the first cid from a BV id via Bilibili API."""
    try:
        resp = await http_client.get(BILIBILI_VIEW_API, params={"bvid": bvid})
        data = resp.json()
        if data.get("code") == 0:
            pages = data["data"]["pages"]
            if pages:
                return str(pages[0]["cid"])
    except Exception:
        pass
    return None





async def _handle_danmaku(
    cid: str,
    font: str,
    font_size: float,
    alpha: float,
    display_area: float,
    duration_marquee: float,
    duration_still: float,
) -> Response:
    """Fetch XML danmaku from Bilibili and convert to ASS."""
    xml_url = BILIBILI_DANMAKU_URL.format(cid=cid)
    try:
        resp = await http_client.get(xml_url)
        resp.raise_for_status()
    except Exception as e:
        return PlainTextResponse(f"获取弹幕失败: {e}", status_code=502)

    xml_bytes = resp.content
    danmakus = _parse_danmaku_xml(xml_bytes)
    ass_text = _danmakus_to_ass(
        danmakus,
        font=font,
        font_size=font_size,
        alpha=alpha,
        display_area=display_area,
        duration_marquee=duration_marquee,
        duration_still=duration_still,
    )
    return PlainTextResponse(
        ass_text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"inline; filename=danmaku_{cid}.ass"},
    )





async def _handle_subtitle_proxy(
    url: str, font: str, font_size: float
) -> Response:
    """Fetch Bilibili JSON subtitle and convert to ASS."""
    try:
        resp = await http_client.get(url)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return PlainTextResponse(f"获取字幕失败: {e}", status_code=502)

    ass_text = _subtitle_json_to_ass(data, font=font, font_size=font_size)
    return PlainTextResponse(
        ass_text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": "inline; filename=subtitle.ass"},
    )





def _parse_danmaku_xml(xml_bytes: bytes) -> list[dict]:
    """
    Parse Bilibili danmaku XML.
    <d p="time,type,fontSize,color,timestamp,pool,uid,dmid">content</d>

    type: 1/2/3 = scroll (R→L), 4 = bottom, 5 = top, 6 = R→L, 7 = special, 8 = code
    """
    danmakus = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return danmakus

    for d in root.iter("d"):
        p = d.get("p", "")
        text = d.text
        if not p or not text:
            continue
        parts = p.split(",")
        if len(parts) < 4:
            continue
        try:
            danmakus.append(
                {
                    "time": float(parts[0]),
                    "type": int(parts[1]),
                    "size": int(parts[2]),
                    "color": int(parts[3]),
                    "text": text.strip(),
                }
            )
        except (ValueError, IndexError):
            continue

    danmakus.sort(key=lambda x: x["time"])
    return danmakus





ASS_WIDTH = 1920
ASS_HEIGHT = 1080


def _color_to_ass(color_int: int) -> str:
    """
    Convert integer color (0xRRGGBB) to ASS color format (&HBBGGRR&).
    """
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8) & 0xFF
    b = color_int & 0xFF
    return f"&H{b:02X}{g:02X}{r:02X}&"


def _alpha_to_ass(opacity: float) -> str:
    """
    Convert opacity (0.0-1.0) to ASS alpha tag.
    0.0 = fully transparent → &HFF
    1.0 = fully opaque → &H00
    """
    a = int((1.0 - max(0.0, min(1.0, opacity))) * 255)
    return f"&H{a:02X}&"


def _seconds_to_ass_time(seconds: float) -> str:
    """Convert seconds to ASS time format H:MM:SS.CC (centiseconds)."""
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        cs = 99
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _danmakus_to_ass(
    danmakus: list[dict],
    font: str = "微软雅黑",
    font_size: float = 30.0,
    alpha: float = 0.8,
    display_area: float = 0.8,
    duration_marquee: float = 15.0,
    duration_still: float = 15.0,
) -> str:
    """
    Convert parsed danmaku list to a full ASS subtitle string.

    Uses lane-based collision avoidance to prevent danmaku overlap.
    """
    alpha_tag = _alpha_to_ass(alpha)


    usable_height = int(ASS_HEIGHT * max(0.1, min(1.0, display_area)))
    lane_height = int(font_size * 1.2)
    num_lanes = max(1, usable_height // lane_height)


    scroll_lanes = [0.0] * num_lanes  # end time when lane is free
    top_lanes = [0.0] * num_lanes
    bottom_lanes = [0.0] * num_lanes

    dialogues = []

    for dm in danmakus:
        dm_type = dm["type"]
        dm_time = dm["time"]
        dm_text = dm["text"]
        dm_color = dm["color"]
        dm_size = dm["size"]


        if dm_type not in (1, 2, 3, 4, 5, 6):
            continue

        color_tag = _color_to_ass(dm_color)

        scale = dm_size / 25.0
        actual_size = int(font_size * scale)

        if dm_type in (1, 2, 3, 6):

            duration = duration_marquee
            end_time = dm_time + duration


            text_len = len(dm_text)
            text_width = text_len * actual_size


            lane = _find_scroll_lane(
                scroll_lanes, dm_time, duration, text_width, num_lanes
            )
            y = lane * lane_height + lane_height // 2


            clear_time = dm_time + duration * text_width / (ASS_WIDTH + text_width)
            scroll_lanes[lane] = clear_time


            x1 = ASS_WIDTH + text_width // 2
            x2 = -(text_width // 2)

            move_tag = f"\\move({x1},{y},{x2},{y})"
            style_tag = (
                f"{{{move_tag}\\alpha{alpha_tag}\\1c{color_tag}"
                f"\\fs{actual_size}\\fn{font}\\bord1.5\\shad0\\q2}}"
            )

            start_str = _seconds_to_ass_time(dm_time)
            end_str = _seconds_to_ass_time(end_time)
            dialogues.append(
                f"Dialogue: 0,{start_str},{end_str},Danmaku,,0,0,0,,{style_tag}{dm_text}"
            )

        elif dm_type == 5:

            duration = duration_still
            end_time = dm_time + duration
            lane = _find_static_lane(top_lanes, dm_time, end_time, num_lanes)
            y = lane * lane_height + lane_height // 2

            pos_tag = f"\\an8\\pos({ASS_WIDTH // 2},{y})"
            style_tag = (
                f"{{{pos_tag}\\alpha{alpha_tag}\\1c{color_tag}"
                f"\\fs{actual_size}\\fn{font}\\bord1.5\\shad0\\q2}}"
            )
            top_lanes[lane] = end_time

            start_str = _seconds_to_ass_time(dm_time)
            end_str = _seconds_to_ass_time(end_time)
            dialogues.append(
                f"Dialogue: 0,{start_str},{end_str},Danmaku,,0,0,0,,{style_tag}{dm_text}"
            )

        elif dm_type == 4:

            duration = duration_still
            end_time = dm_time + duration
            lane = _find_static_lane(bottom_lanes, dm_time, end_time, num_lanes)

            y = ASS_HEIGHT - lane * lane_height - lane_height // 2

            pos_tag = f"\\an2\\pos({ASS_WIDTH // 2},{y})"
            style_tag = (
                f"{{{pos_tag}\\alpha{alpha_tag}\\1c{color_tag}"
                f"\\fs{actual_size}\\fn{font}\\bord1.5\\shad0\\q2}}"
            )
            bottom_lanes[lane] = end_time

            start_str = _seconds_to_ass_time(dm_time)
            end_str = _seconds_to_ass_time(end_time)
            dialogues.append(
                f"Dialogue: 0,{start_str},{end_str},Danmaku,,0,0,0,,{style_tag}{dm_text}"
            )

    return _build_ass_file(font, font_size, dialogues)


def _find_scroll_lane(
    lanes: list[float],
    start: float,
    duration: float,
    text_width: int,
    num_lanes: int,
) -> int:
    """Find the best lane for a scrolling danmaku (no overlap)."""
    for i in range(num_lanes):
        if lanes[i] <= start:
            return i

    return min(range(num_lanes), key=lambda i: lanes[i])


def _find_static_lane(
    lanes: list[float], start: float, end: float, num_lanes: int
) -> int:
    """Find the best lane for a static danmaku."""
    for i in range(num_lanes):
        if lanes[i] <= start:
            return i
    return min(range(num_lanes), key=lambda i: lanes[i])


def _build_ass_file(font: str, font_size: float, dialogues: list[str]) -> str:
    """Build a complete ASS file with header, styles, and dialogue lines."""
    header = f"""[Script Info]
Title: Bilibili Danmaku
ScriptType: v4.00+
PlayResX: {ASS_WIDTH}
PlayResY: {ASS_HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Danmaku,{font},{int(font_size)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1.5,0,2,0,0,0,1
Style: Subtitle,{font},{int(font_size)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,10,10,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    return header + "\n".join(dialogues) + "\n"





def _subtitle_json_to_ass(
    data: dict, font: str = "微软雅黑", font_size: float = 30.0
) -> str:
    """
    Convert Bilibili JSON subtitle to ASS format.
    JSON structure: {"body": [{"from": start, "to": end, "content": text}, ...]}
    """
    dialogues = []
    body = data.get("body", [])
    for item in body:
        start = item.get("from", 0)
        end = item.get("to", 0)
        content = item.get("content", "")
        if not content:
            continue

        content = content.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        start_str = _seconds_to_ass_time(start)
        end_str = _seconds_to_ass_time(end)
        dialogues.append(
            f"Dialogue: 0,{start_str},{end_str},Subtitle,,0,0,0,,{content}"
        )

    return _build_ass_file(font, font_size, dialogues)




if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
