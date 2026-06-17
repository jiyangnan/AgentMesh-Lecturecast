#!/usr/bin/env python3
"""TTS with degradation: MiniMax (default) → Edge TTS Yunjian (fallback).

Per sentence synth + ffmpeg concat (avoids long-segment truncation, gives accurate cues).
Rule:
  - default engine = MiniMax T2A (voice male-qn-jingying, speed 1.0)
  - network/transient error → retry 3x, 20s apart
  - quota/balance/auth error (清晰额度问题) → immediate fallback, no waiting
  - once fallback triggered, all remaining sentences use Edge zh-CN-YunjianNeural (+8%)
Key (BYOK): your own MiniMax key from env MINIMAX_API_KEY. No key → Edge-only.
"""
import asyncio, json, os, subprocess, sys, time
from pathlib import Path
import urllib.request
import edge_tts

ROOT = Path(__file__).parent
SCRIPT = json.loads((ROOT / "scripts" / "bilibili.json").read_text())
AUDIO = ROOT / "audio"; AUDIO.mkdir(exist_ok=True)
TMP = AUDIO / "_parts"; TMP.mkdir(exist_ok=True)

MM_VOICE = "male-qn-jingying"
MM_MODEL = "speech-02-hd"
MM_SPEED = 1.0
EDGE_VOICE = "zh-CN-YunjianNeural"
EDGE_RATE = "+8%"
RETRIES = 3
RETRY_WAIT = 20  # seconds

def get_key():
    # BYOK: read your own MiniMax key from the environment only. No fallback
    # path — set `export MINIMAX_API_KEY=...` to use MiniMax, else Edge is used.
    k = os.environ.get("MINIMAX_API_KEY")
    return k.strip() if k else None

MM_KEY = get_key()
STATE = {"engine": "minimax" if MM_KEY else "edge"}
QUOTA_CODES = {1008, 1004, 2049, 1004001}  # balance / auth / invalid key

def ffdur(p):
    out = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",str(p)],
                         capture_output=True, text=True).stdout.strip()
    return float(out) if out else 0.0

def split_sentences(text):
    parts, buf = [], ""
    for ch in text:
        buf += ch
        if ch in "。！？":
            parts.append(buf.strip()); buf = ""
    if buf.strip(): parts.append(buf.strip())
    return [p for p in parts if p]

class Quota(Exception): pass

def mm_once(text):
    body = json.dumps({"model": MM_MODEL, "text": text, "stream": False,
        "voice_setting": {"voice_id": MM_VOICE, "speed": MM_SPEED, "vol": 1, "pitch": 0},
        "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1}}).encode()
    req = urllib.request.Request("https://api.minimaxi.com/v1/t2a_v2", data=body,
        headers={"Authorization": f"Bearer {MM_KEY}", "Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=45).read())
    sc = d.get("base_resp", {}).get("status_code")
    if sc == 0:
        return bytes.fromhex(d["data"]["audio"])
    msg = (d.get("base_resp", {}).get("status_msg") or "")
    if sc in QUOTA_CODES or any(w in msg for w in ("余额", "balance", "insufficient", "auth", "key")):
        raise Quota(f"{sc}:{msg}")
    raise RuntimeError(f"mm status {sc}:{msg}")  # transient → retry

def mm_synth(text):
    last = None
    for attempt in range(RETRIES):
        try:
            return mm_once(text)
        except Quota:
            raise  # immediate fallback, no wait
        except Exception as e:
            last = e
            if attempt < RETRIES - 1:
                print(f"    ⚠ MiniMax 第{attempt+1}次失败({str(e)[:40]})，{RETRY_WAIT}s后重试")
                time.sleep(RETRY_WAIT)
    raise RuntimeError(f"mm exhausted: {last}")

async def edge_synth(text):
    c = edge_tts.Communicate(text, EDGE_VOICE, rate=EDGE_RATE)
    buf = bytearray()
    async for ch in c.stream():
        if ch["type"] == "audio":
            buf += ch["data"]
    return bytes(buf)

async def synth_sentence(text, out):
    if STATE["engine"] == "minimax":
        try:
            data = mm_synth(text)
            out.write_bytes(data)
            return ffdur(out), "minimax"
        except Quota as e:
            print(f"  ⚠ MiniMax 额度/鉴权问题（{e}）→ 永久回退 Edge Yunjian")
            STATE["engine"] = "edge"
        except Exception as e:
            print(f"  ⚠ MiniMax 三次重试仍不通（{str(e)[:50]}）→ 回退 Edge Yunjian")
            STATE["engine"] = "edge"
    data = await edge_synth(text)
    out.write_bytes(data)
    return ffdur(out), "edge"

async def main():
    print(f"默认引擎: {STATE['engine']}  (MiniMax voice={MM_VOICE}, fallback=Edge {EDGE_VOICE})")
    total = 0.0; engines = set()
    for s in SCRIPT["sections"]:
        sid = s["id"]; sents = split_sentences(s["text"])
        cues, cum, parts = [], 0.0, []
        for i, sent in enumerate(sents):
            pf = TMP / f"{sid}_{i:02d}.mp3"
            d, eng = await synth_sentence(sent, pf)
            engines.add(eng)
            cues.append({"start": round(cum, 3), "end": round(cum + d, 3), "text": sent})
            cum += d; parts.append(pf)
        listf = TMP / f"{sid}.txt"
        listf.write_text("\n".join(f"file '{p.name}'" for p in parts))
        out = AUDIO / f"{sid}.mp3"
        subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(listf),"-c","copy",str(out)],
                       capture_output=True, cwd=str(TMP))
        dur = ffdur(out)
        (AUDIO / f"{sid}.json").write_text(json.dumps({"section": sid, "duration": dur, "cues": cues}, ensure_ascii=False, indent=2))
        print(f"  {sid}: {dur:.2f}s ({len(cues)} sentences)")
        total += dur
    print(f"\nTotal: {total:.2f}s ({total/60:.2f} min)  engines used: {engines}")

if __name__ == "__main__":
    asyncio.run(main())
