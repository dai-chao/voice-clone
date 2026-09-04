#!/usr/bin/env python3
"""MiniMax 音色复刻本地服务：上传音频到百炼临时存储，再调用 voice_clone / TTS。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, NoReturn

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DASHSCOPE = "https://dashscope.aliyuncs.com/api/v1"
GENERATION_URL = f"{DASHSCOPE}/services/aigc/multimodal-generation/generation"
UPLOAD_POLICY_URL = f"{DASHSCOPE}/uploads"

ALLOWED_SUFFIX = {".mp3", ".m4a", ".wav"}
MAX_BYTES = 20 * 1024 * 1024
MIN_SECONDS = 10
MAX_SECONDS = 295  # MiniMax 上限 5 分钟，留一点余量
VOICE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{6,254}[A-Za-z0-9]$")

CLONE_STATUS = {
    0: "成功",
    1000: "未知错误",
    1001: "请求超时，请稍后重试",
    1002: "触发限流，请稍后重试",
    1004: "鉴权失败，请检查 API Key",
    1013: "服务内部错误，请稍后重试",
    1039: "触发 TPM 限流，请稍后重试",
    1042: "非法字符超过 10%，请检查试听文案",
    2013: "输入参数不正常（检查音频格式、时长或 voice_id）",
    2038: "无复刻权限，请检查账号认证状态",
}

ERROR_ZH = (
    ("voice clone voice id duplicate", "音色 ID 已存在，请更换一个新的 voice_id 后再试"),
    ("voice id duplicate", "音色 ID 已存在，请更换一个新的 voice_id 后再试"),
    ("duplicate", "音色 ID 已存在，请更换后再试"),
    ("duration too long", "音频超过 5 分钟。请换一段不超过 5 分钟的录音"),
    ("too long", "音频过长。复刻音频需在 10 秒到 5 分钟之间"),
    ("too short", "音频过短。复刻至少需要 10 秒"),
    ("invalid api-key", "API Key 无效，请检查后重试"),
    ("invalid api key", "API Key 无效，请检查后重试"),
    ("invalidapikey", "API Key 无效，请检查后重试"),
    ("unauthorized", "鉴权失败，请检查 API Key"),
    ("throttl", "请求过于频繁，请稍后重试"),
    ("rate limit", "触发限流，请稍后重试"),
    ("timeout", "请求超时，请稍后重试"),
    ("sensitive", "内容命中风控，请更换音频或文案"),
    ("no permission", "没有复刻权限，请检查账号认证状态"),
    ("invalid parameter", "参数不正确，请检查音频、文案和 voice_id"),
    ("file format", "音频格式不支持，请使用 mp3、m4a 或 wav"),
    ("not found", "资源不存在"),
)


def to_zh(text: str, code: int | None = None) -> str:
    raw = (text or "").strip()
    lowered = raw.lower()
    for needle, zh in ERROR_ZH:
        if needle in lowered:
            return zh
    if code in CLONE_STATUS and CLONE_STATUS[code] != "成功":
        base = CLONE_STATUS[code]
        if raw and raw.lower() not in {base.lower(), "success"}:
            return f"{base}：{raw}" if not any("\u4e00" <= ch <= "\u9fff" for ch in raw) else base
        return base
    if raw and all(ord(ch) < 128 for ch in raw):
        return f"复刻失败：{raw}"
    return raw or "复刻失败，请稍后重试"

app = FastAPI(title="MiniMax 音色复刻")


def http() -> requests.Session:
    """直连百炼，避开 Cursor/系统 HTTP 代理对 *.aliyuncs.com 的 403。"""
    session = requests.Session()
    session.trust_env = False
    return session


def raise_network_error(action: str, exc: Exception) -> NoReturn:
    raise HTTPException(502, f"{action}失败：无法连接百炼（{exc}）。请确认本机可访问 dashscope.aliyuncs.com") from exc


def resolve_key(explicit: str | None) -> str:
    key = (explicit or "").strip() or (os.getenv("DASHSCOPE_API_KEY") or "").strip()
    if not key:
        raise HTTPException(400, "请填写百炼 API Key，或设置环境变量 DASHSCOPE_API_KEY")
    return key


def dashscope_headers(api_key: str, *, clone: bool = False, oss: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8" if clone else "application/json",
    }
    if oss:
        headers["X-DashScope-OssResourceResolve"] = "enable"
    return headers


def raise_if_clone_failed(payload: dict[str, Any]) -> None:
    output = payload.get("output") or {}
    base = output.get("base_resp") or {}
    code = base.get("status_code", 0)
    if code in (None, 0):
        return
    raise HTTPException(400, to_zh(base.get("status_msg") or "", code))


def get_upload_policy(api_key: str, model: str) -> dict[str, Any]:
    try:
        response = http().get(
            UPLOAD_POLICY_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            params={"action": "getPolicy", "model": model},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise_network_error("获取上传凭证", exc)
    if response.status_code != 200:
        raise HTTPException(
            response.status_code,
            f"获取上传凭证失败：{response.text[:500]}",
        )
    data = response.json().get("data")
    if not data:
        raise HTTPException(502, f"上传凭证响应异常：{response.text[:500]}")
    return data


def upload_to_dashscope_oss(policy: dict[str, Any], filename: str, content: bytes) -> str:
    key = f"{policy['upload_dir']}/{filename}"
    files = {
        "OSSAccessKeyId": (None, policy["oss_access_key_id"]),
        "Signature": (None, policy["signature"]),
        "policy": (None, policy["policy"]),
        "x-oss-object-acl": (None, policy["x_oss_object_acl"]),
        "x-oss-forbid-overwrite": (None, str(policy["x_oss_forbid_overwrite"])),
        "key": (None, key),
        "success_action_status": (None, "200"),
        "file": (filename, content),
    }
    try:
        response = http().post(policy["upload_host"], files=files, timeout=120)
    except requests.RequestException as exc:
        raise_network_error("上传音频到临时存储", exc)
    if response.status_code != 200:
        raise HTTPException(502, f"音频上传到临时存储失败：{response.text[:500]}")
    return f"oss://{key}"


def _bin(name: str) -> str | None:
    return shutil.which(name)


def probe_duration(path: Path) -> float:
    ffprobe = _bin("ffprobe")
    if not ffprobe:
        return 0.0
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return float((result.stdout or "").strip() or 0)
    except ValueError:
        return 0.0


def prepare_clone_audio(content: bytes, suffix: str) -> tuple[bytes, str, bool]:
    """过短直接报错；超过 5 分钟则截取前 4 分 55 秒。返回 (bytes, suffix, trimmed)。"""
    ffmpeg = _bin("ffmpeg")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"src{suffix}"
        src.write_bytes(content)
        duration = probe_duration(src)
        if 0 < duration < MIN_SECONDS:
            raise HTTPException(400, f"音频只有 {duration:.1f} 秒，复刻至少需要 10 秒")
        if duration and duration <= MAX_SECONDS:
            return content, suffix, False
        if not ffmpeg:
            raise HTTPException(400, "音频超过 5 分钟。请先剪到 10 秒–5 分钟，或安装 ffmpeg 以便自动截取")
        out = Path(tmp) / "clip.mp3"
        cmd = [
            ffmpeg, "-y", "-i", str(src),
            "-t", str(MAX_SECONDS),
            "-ac", "1", "-ar", "44100",
            "-c:a", "libmp3lame", "-b:a", "128k",
            str(out),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not out.exists():
            raise HTTPException(400, f"自动截取音频失败：{(result.stderr or '')[-400:]}")
        clipped = out.read_bytes()
        if len(clipped) > MAX_BYTES:
            raise HTTPException(400, "截取后的音频仍然超过 20 MB")
        return clipped, ".mp3", True


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/clone")
async def clone_voice(
    api_key: str = Form(""),
    model: str = Form("MiniMax/speech-2.8-hd"),
    voice_id: str = Form(...),
    text: str = Form("你好，这是用复刻音色生成的试听。"),
    language_boost: str = Form("auto"),
    need_noise_reduction: bool = Form(False),
    need_volume_normalization: bool = Form(False),
    audio_url: str = Form(""),
    file: UploadFile | None = File(None),
) -> dict[str, Any]:
    key = resolve_key(api_key)
    voice_id = voice_id.strip()
    if not VOICE_ID_RE.match(voice_id):
        raise HTTPException(
            400,
            "voice_id 需 8–256 位，以字母开头，以字母或数字结尾，中间只能是字母、数字、连字符或下划线",
        )
    text = text.strip()
    if not text or len(text) > 1000:
        raise HTTPException(400, "试听文案必填，且不超过 1000 字")

    oss = False
    trimmed = False
    source_url = audio_url.strip()
    if file and file.filename:
        suffix = Path(file.filename).suffix.lower()
        if suffix not in ALLOWED_SUFFIX:
            raise HTTPException(400, "仅支持 mp3 / m4a / wav")
        content = await file.read()
        if not content:
            raise HTTPException(400, "上传的音频是空文件")
        if len(content) > MAX_BYTES:
            raise HTTPException(400, "音频不能超过 20 MB")
        content, suffix, trimmed = prepare_clone_audio(content, suffix)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(file.filename).stem)[:80] or "audio"
        filename = f"{uuid.uuid4().hex[:10]}_{safe_name}{suffix}"
        policy = get_upload_policy(key, model)
        source_url = upload_to_dashscope_oss(policy, filename, content)
        oss = True
    elif not source_url:
        raise HTTPException(400, "请上传音频文件，或填写公网可访问的音频 URL")

    payload = {
        "model": model,
        "input": {
            "action": "voice_clone",
            "voice_id": voice_id,
            "audio_url": source_url,
            "text": text,
            "need_noise_reduction": need_noise_reduction,
            "need_volume_normalization": need_volume_normalization,
            "aigc_watermark": False,
        },
    }
    if language_boost:
        payload["input"]["language_boost"] = language_boost

    try:
        response = http().post(
            GENERATION_URL,
            headers=dashscope_headers(key, clone=True, oss=oss),
            json=payload,
            timeout=180,
        )
    except requests.RequestException as exc:
        raise_network_error("调用复刻接口", exc)

    if response.status_code != 200:
        raise HTTPException(response.status_code, f"复刻接口错误：{response.text[:800]}")

    body = response.json()
    raise_if_clone_failed(body)
    output = body.get("output") or {}
    return {
        "voice_id": voice_id,
        "model": model,
        "demo_audio": output.get("demo_audio"),
        "input_sensitive": output.get("input_sensitive", False),
        "request_id": body.get("request_id"),
        "usage": body.get("usage"),
        "audio_url": source_url,
        "trimmed": trimmed,
    }


class TtsRequest(BaseModel):
    api_key: str = ""
    model: str = "MiniMax/speech-2.8-hd"
    voice_id: str
    text: str = Field(..., min_length=1, max_length=9999)
    speed: float = 1.0
    vol: float = 1.0
    pitch: int = 0
    emotion: str = ""


@app.post("/api/tts")
def synthesize(req: TtsRequest) -> dict[str, Any]:
    key = resolve_key(req.api_key)
    voice_setting: dict[str, Any] = {
        "voice_id": req.voice_id.strip(),
        "speed": req.speed,
        "vol": req.vol,
        "pitch": req.pitch,
    }
    if req.emotion:
        voice_setting["emotion"] = req.emotion
    payload = {
        "model": req.model,
        "input": {
            "text": req.text.strip(),
            "voice_setting": voice_setting,
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": "mp3",
                "channel": 1,
            },
            "output_format": "url",
            "subtitle_enable": False,
        },
    }
    try:
        response = http().post(
            GENERATION_URL,
            headers=dashscope_headers(key, clone=False),
            json=payload,
            timeout=180,
        )
    except requests.RequestException as exc:
        raise_network_error("调用合成接口", exc)

    if response.status_code != 200:
        raise HTTPException(response.status_code, f"合成接口错误：{response.text[:800]}")

    body = response.json()
    raise_if_clone_failed(body)
    output = body.get("output") or {}
    data = output.get("data") or {}
    audio = data.get("audio") or output.get("audio")
    if not audio:
        raise HTTPException(502, "合成成功但没有返回音频")
    extra = output.get("extra_info") or {}
    return {
        "audio": audio,
        "request_id": body.get("request_id"),
        "usage": body.get("usage"),
        "extra_info": extra,
    }


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8765"))
    reload = os.getenv("RELOAD", "0") == "1"
    uvicorn.run("server:app", host=host, port=port, reload=reload)
