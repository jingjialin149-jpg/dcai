import os
import re
import asyncio
import io
import wave
import urllib.request
import xml.etree.ElementTree as ET
import html
import json
import hashlib
from datetime import datetime, timezone, timedelta

# 1. 自動下載並掛載 FFmpeg 解碼核心
import static_ffmpeg
static_ffmpeg.add_paths()

import discord
from discord import opus
from discord.ext import tasks, voice_recv
from google import genai
from google.genai import types
from google.genai.errors import APIError
import edge_tts

# 2. 自動載入系統中的 Opus 語音庫
if not opus.is_loaded():
    opus_libs = [
        "libopus.so.0",
        "libopus.so",
        "libopus-0.x86_64.so",
        "libopus.0.dylib",
        "opus"
    ]
    for opus_lib in opus_libs:
        try:
            opus.load_opus(opus_lib)
            if opus.is_loaded():
                print(f"✅ 成功載入 Opus 庫：{opus_lib}")
                break
        except Exception:
            continue

# ==================== 1. 環境變數與金鑰設定 ====================
raw_keys = os.getenv("GEMINI_API_KEY", "")
API_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

current_key_index = 0

def get_current_client():
    global current_key_index
    if not API_KEYS:
        raise ValueError("未設定任何 GEMINI_API_KEY")
    return genai.Client(api_key=API_KEYS[current_key_index])

def switch_to_next_key():
    global current_key_index
    if len(API_KEYS) > 1:
        current_key_index = (current_key_index + 1) % len(API_KEYS)
        print(f"已自動切換至金鑰 index: {current_key_index}")
        return True
    return False

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
client = discord.Client(intents=intents)

MODELS_TO_TRY = ["gemini-3.8-flash", "gemini-3-flash-preview"]
TTS_VOICE = "zh-TW-HsiaoChenNeural"

# ==================== 2. 自訂人設與個性設定 ====================
DEFAULT_PERSONA = os.getenv(
    "BOT_PERSONA",
    "你是一個友善、聰明且有問必答的智慧助理。回覆時請用自然親切的口吻，並以繁體中文回答。"
)
custom_persona = DEFAULT_PERSONA

SAFETY_SETTINGS = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
]

# ==================== 3. 語音錄音與音訊處理 ====================
class AudioBufferSink(voice_recv.AudioSink):
    def __init__(self):
        super().__init__()
        self.byte_buffer = bytearray()

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data):
        if data.pcm:
            self.byte_buffer.extend(data.pcm)

    def cleanup(self):
        self.byte_buffer.clear()

async def pcm_to_wav(pcm_data: bytes) -> bytes:
    wav_io = io.BytesIO()
    with wave.open(wav_io, "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(48000)
        wav_file.writeframes(pcm_data)
    wav_io.seek(0)
    return wav_io.read()

async def text_to_speech(text: str, filename="reply.mp3"):
    clean_speech = re.sub(r"[*_~`#>\-]+", "", text).strip()
    communicate = edge_tts.Communicate(clean_speech, TTS_VOICE)
    await communicate.save(filename)

def play_audio_in_vc(vc, filename: str):
    if vc and vc.is_connected():
        if vc.is_playing():
            vc.stop()
        vc.play(discord.FFmpegPCMAudio(filename))

# ==================== 4. 監控模組（支援持久化儲存） ====================
MONITORS_FILE = "monitors.json"
tracked_targets = {}

def load_monitors():
    global tracked_targets
    if os.path.exists(MONITORS_FILE):
        try:
            with open(MONITORS_FILE, "r", encoding="utf-8") as f:
                tracked_targets = json.load(f)
                print(f"✅ 成功載入 {len(tracked_targets)} 個監控項目！")
        except Exception as e:
            print(f"讀取監控檔案失敗：{e}")
            tracked_targets = {}
    else:
        tracked_targets = {}

def save_monitors():
    try:
        with open(MONITORS_FILE, "w", encoding="utf-8") as f:
            json.dump(tracked_targets, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"儲存監控項目失敗：{e}")

def clean_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw_html)
    return html.unescape(text).strip()

def extract_twitter_user(text: str) -> str:
    match = re.search(r"(?:twitter\.com|x\.com)/([A-Za-z0-9_]+)", text)
    if match:
        return match.group(1)
    clean = text.replace("@", "").strip()
    if re.match(r"^[A-Za-z0-9_]+$", clean) and not clean.startswith("http"):
        return clean
    return None

async def fetch_url_data(url: str) -> bytes:
    loop = asyncio.get_event_loop()
    def _fetch():
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.read()
    return await loop.run_in_executor(None, _fetch)

def parse_web_or_feed(raw_data: bytes, source_url: str):
    text = raw_data.decode("utf-8", errors="ignore")

    if "<rss" in text.lower() or "<feed" in text.lower():
        try:
            tree = ET.fromstring(raw_data)
            item = tree.find(".//item")
            if item is not None:
                title = item.findtext("title") or "新內容"
                link = item.findtext("link") or source_url
                desc = clean_html(item.findtext("description") or "")
                return "feed", (link or title), title, desc[:200], link

            entry = None
            for elem in tree.iter():
                if elem.tag.endswith("entry"):
                    entry = elem
                    break
            if entry is not None:
                title, link, desc = "新內容", source_url, ""
                for child in entry:
                    tag = child.tag.split("}")[-1]
                    if tag == "title":
                        title = child.text or title
                    elif tag == "link":
                        link = child.attrib.get("href", link)
                    elif tag in ("content", "summary"):
                        desc = clean_html(child.text or "")
                return "feed", (link or title), title, desc[:200], link
        except Exception:
            pass

    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    page_title = html.unescape(title_match.group(1).strip()) if title_match else source_url
    no_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    clean_text = clean_html(no_scripts)
    snippet = re.sub(r"\s+", " ", clean_text)[:150].strip()
    content_hash = hashlib.md5(clean_text[:4000].encode("utf-8")).hexdigest()
    return "web", content_hash, page_title, snippet, source_url

async def scan_single_target(key: str, info: dict, channel: discord.TextChannel) -> bool:
    if info.get("is_twitter"):
        username = info["twitter_user"]
        endpoints = [
            f"https://rsshub.app/twitter/user/{username}",
            f"https://nitter.net/{username}/rss",
            f"https://nitter.privacydev.net/{username}/rss",
        ]
        for ep in endpoints:
            try:
                raw_data = await fetch_url_data(ep)
                _, unique_id, title, desc, link = parse_web_or_feed(raw_data, ep)
                
                if info.get("last_id") is None:
                    tracked_targets[key]["last_id"] = unique_id
                    save_monitors()
                    return False

                if unique_id != info.get("last_id"):
                    tracked_targets[key]["last_id"] = unique_id
                    save_monitors()
                    msg = (
                        f"🐦 **@{username} 發布了新推文！**\n\n"
                        f"{desc}\n\n"
                        f"🔗 **推文連結**：<{link}>"
                    )
                    await channel.send(msg)
                    return True
                return False
            except Exception:
                continue
        return False

    try:
        raw_data = await fetch_url_data(info["url"])
        kind, unique_id, title, desc, link = parse_web_or_feed(raw_data, info["url"])

        if info.get("last_id") is None:
            tracked_targets[key]["last_id"] = unique_id
            save_monitors()
            return False

        if unique_id != info.get("last_id"):
            tracked_targets[key]["last_id"] = unique_id
            save_monitors()
            if kind == "feed":
                msg = f"📢 **【更新通知】{title}**\n\n{desc}\n\n🔗 **傳送門**：<{link}>"
            else:
                msg = f"🌐 **【網頁更新通知】{title}**\n\n📝 **最新摘要**：\n{desc}...\n\n🔗 **網址**：<{link}>"
            await channel.send(msg)
            return True
    except Exception as e:
        print(f"掃描 {info['url']} 失敗：{e}")
    return False

@tasks.loop(minutes=3)
async def check_all_monitors():
    if not tracked_targets:
        return

    for key, info in list(tracked_targets.items()):
        channel = client.get_channel(info["channel_id"])
        if not channel:
            continue
        await scan_single_target(key, info, channel)

# ==================== 5. 智慧提醒排程器 ====================
async def schedule_reminder(delay_seconds: int, channel_id: int, user_id: int, task_description: str):
    await asyncio.sleep(delay_seconds)
    channel = client.get_channel(channel_id)
    if channel:
        await channel.send(
            f"⏰ <@{user_id}> **叮咚！提醒時間到了：**\n"
            f"> {task_description}"
        )

# ==================== 6. Discord 事件監聽 ====================
@client.event
async def on_ready():
    print(f"萬能 AI 機器人已上線：{client.user}")
    print(f"目前共載入 {len(API_KEYS)} 把 API Key 備援")
    load_monitors()
    if not check_all_monitors.is_running():
        check_all_monitors.start()

@client.event
async def on_message(message):
    global custom_persona
    if message.author == client.user:
        return

    raw_text = message.content.strip()
    clean_text = re.sub(r"^<@!?\d+>\s*", "", raw_text).strip()

    # ---------------- 人設管理指令 ----------------
    if clean_text.startswith("!persona ") or clean_text.startswith("!設定 "):
        parts = clean_text.split(" ", 1)
        new_persona = parts[1].strip()
        if new_persona.startswith("<") and new_persona.endswith(">"):
            new_persona = new_persona[1:-1].strip()
        custom_persona = new_persona
        await message.reply(f"🎭 **已成功更新機器人人設！**\n\n當前設定：\n> {custom_persona}")
        return

    if clean_text in ("!persona", "!設定"):
        await message.reply(
            f"🎭 **目前的人設與行為設定：**\n> {custom_persona}\n\n"
            "💡 **如何修改？** 輸入：`!persona <你的設定內容>`\n"
            "💡 **如何還原？** 輸入：`!resetpersona`"
        )
        return

    if clean_text == "!resetpersona":
        custom_persona = DEFAULT_PERSONA
        await message.reply("🔄 **已重設為預設人設！**")
        return

    # ---------------- 語音頻道控制 ----------------
    if clean_text == "!join":
        if not message.author.voice:
            await message.reply("⚠️ 請先進入任意一個語音頻道！")
            return
        v_channel = message.author.voice.channel
        if message.guild.voice_client:
            await message.guild.voice_client.move_to(v_channel)
        else:
            await v_channel.connect(cls=voice_recv.VoiceRecvClient)
        await message.reply(f"🔊 已進入語音頻道：**{v_channel.name}**")
        return

    if clean_text == "!leave":
        if message.guild.voice_client:
            await message.guild.voice_client.disconnect()
            await message.reply("👋 已離開語音頻道。")
        else:
            await message.reply("⚠️ 機器人目前不在語音頻道中。")
        return

    # ---------------- 語音朗讀：!say ----------------
    if clean_text.startswith("!say "):
        text_to_read = clean_text[5:].strip()
        if not text_to_read:
            await message.reply("⚠️ 請輸入要朗讀的內容，例如：`!say 大家好！`")
            return

        vc = message.guild.voice_client if message.guild else None
        if not vc or not vc.is_connected():
            await message.reply("⚠️ 請先輸入 `!join` 讓我進入語音頻道！")
            return

        await text_to_speech(text_to_read, "say.mp3")
        play_audio_in_vc(vc, "say.mp3")
        await message.add_reaction("🔊")
        return

    # ---------------- 麥克風全語音對話：!talk ----------------
    if clean_text == "!talk":
        vc = message.guild.voice_client if message.guild else None
        if not vc or not vc.is_connected():
            await message.reply("⚠️ 請先輸入 `!join` 讓我進入語音頻道！")
            return

        status_msg = await message.reply("🎙️ **正在聆聽中... 請對著麥克風說話（限時 7 秒）**")
        sink = AudioBufferSink()
        vc.listen(sink)
        
        await asyncio.sleep(7)
        vc.stop_listening()

        if len(sink.byte_buffer) < 10000:
            await status_msg.edit(content="⚠️ 剛才沒有偵測到足夠的聲音，請再試一次！")
            return

        await status_msg.edit(content="🧠 **正在理解語音並思考回覆中...**")
        wav_bytes = await pcm_to_wav(sink.byte_buffer)
        
        reply_text = None
        for key_round in range(len(API_KEYS)):
            ai_client = get_current_client()
            prompt = (
                f"【你的核心人設】：{custom_persona}\n"
                "這是一段使用者在 Discord 語音頻道對你說的話。請根據你的人設，用繁體中文簡短直接回覆（60字以內）。"
            )
            try:
                response = await ai_client.aio.models.generate_content(
                    model="gemini-3.8-flash",
                    contents=[
                        types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
                        prompt
                    ],
                    config=types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                        safety_settings=SAFETY_SETTINGS
                    )
                )
                reply_text = response.text.strip()
                break
            except Exception as e:
                print(f"語音理解錯誤：{e}")
                if not switch_to_next_key():
                    break

        if not reply_text:
            await status_msg.edit(content="❌ AI 語音理解失敗，請稍後再試。")
            return

        await text_to_speech(reply_text, "reply.mp3")
        await status_msg.edit(content=f"🗣️ **AI 回覆：** {reply_text}")
        play_audio_in_vc(vc, "reply.mp3")
        return

    # ---------------- 萬能網址 / Twitter 監控 ----------------
    if clean_text.startswith("!follow"):
        parts = clean_text.split(" ", 1)
        if len(parts) < 2:
            await message.reply(
                "⚠️ 請附上要監控的網址或帳號，例如：\n"
                "`!follow https://example.com`\n"
                "`!follow https://x.com/帳號`"
            )
            return

        target_input = parts[1].strip()
        tw_user = extract_twitter_user(target_input)
        is_tw = bool(tw_user and ("twitter.com" in target_input or "x.com" in target_input or not target_input.startswith("http")))

        status_msg = await message.reply("🔍 **正在連線並驗證目標網址...**")

        if is_tw:
            user = tw_user
            endpoints = [
                f"https://rsshub.app/twitter/user/{user}",
                f"https://nitter.net/{user}/rss",
                f"https://nitter.privacydev.net/{user}/rss",
            ]
            fetched = False
            last_id = None
            sample_title = ""
            for ep in endpoints:
                try:
                    raw_data = await fetch_url_data(ep)
                    _, unique_id, title, desc, link = parse_web_or_feed(raw_data, ep)
                    last_id = unique_id
                    sample_title = desc[:80] if desc else title
                    fetched = True
                    break
                except Exception:
                    continue

            if not fetched:
                await status_msg.edit(
                    content=f"⚠️ 無法直接連接 Twitter @{user} 的公開源（可能受 X 官方嚴格防爬蟲限制）。\n"
                    "已先將其加入清單，系統仍會在背景每 3 分鐘嘗試輪詢。"
                )
                last_id = None
            else:
                await status_msg.edit(
                    content=f"✅ **成功監控 Twitter @{user}！**\n"
                    f"📌 **目前最新推文**：{sample_title}...\n"
                    f"未來有發新推文將自動在此頻道推播！"
                )

            tracked_targets[f"tw_{user}"] = {
                "url": f"https://x.com/{user}",
                "channel_id": message.channel.id,
                "last_id": last_id,
                "name": f"Twitter @{user}",
                "is_twitter": True,
                "twitter_user": user
            }
            save_monitors()
            return

        if not target_input.startswith("http://") and not target_input.startswith("https://"):
            target_input = "https://" + target_input

        try:
            raw_data = await fetch_url_data(target_input)
            kind, unique_id, title, snippet, link = parse_web_or_feed(raw_data, target_input)
            
            tracked_targets[target_input] = {
                "url": target_input,
                "channel_id": message.channel.id,
                "last_id": unique_id,
                "name": title[:40],
                "is_twitter": False
            }
            save_monitors()

            await status_msg.edit(
                content=f"✅ **成功加入監控清單！**\n"
                f"🌐 **網站名稱**：{title}\n"
                f"📝 **目前內容摘要**：{snippet[:80]}...\n"
                f"系統每 3 分鐘自動巡邏，有更新時會發布在此頻道。"
            )
        except Exception as e:
            await status_msg.edit(
                content=f"❌ **連線失敗，無法加入監控！**\n"
                f"原因：`{e}`\n"
                "提示：該網站可能有 Cloudflare 防爬蟲阻擋或無效網址。"
            )
        return

    # ---------------- 立即手動檢查所有監控：!check ----------------
    if clean_text == "!check":
        if not tracked_targets:
            await message.reply("📋 目前沒有設定任何監控項目。")
            return
        status_msg = await message.reply("🔄 正在立即巡邏檢查所有監控目標...")
        updated_count = 0
        for key, info in list(tracked_targets.items()):
            channel = client.get_channel(info["channel_id"])
            if channel:
                if await scan_single_target(key, info, channel):
                    updated_count += 1
        await status_msg.edit(content=f"✅ 巡邏完成！共檢查了 {len(tracked_targets)} 個目標，發現 {updated_count} 個新更新。")
        return

    # ---------------- 查看監控清單：!following ----------------
    if clean_text == "!following":
        if not tracked_targets:
            await message.reply("📋 目前沒有監控任何網址或社群帳號。")
            return
        msg = "**📋 目前監控清單：**\n"
        for i, (k, data) in enumerate(tracked_targets.items(), 1):
            msg += f"{i}. **{data['name']}** ➔ 推播頻道：<#{data['channel_id']}>\n"
        msg += "\n💡 可輸入 `!check` 立即手動巡邏，或輸入 `!unfollow <編號>` 取消監控。"
        await message.reply(msg)
        return

    # ---------------- 取消監控：!unfollow ----------------
    if clean_text.startswith("!unfollow"):
        parts = clean_text.split(" ", 1)
        if len(parts) < 2:
            await message.reply("⚠️ 請輸入要取消的編號或名稱，例如：`!unfollow 1`")
            return

        arg = parts[1].strip()
        removed_key = None

        if arg.isdigit():
            idx = int(arg) - 1
            keys = list(tracked_targets.keys())
            if 0 <= idx < len(keys):
                removed_key = keys[idx]
        else:
            for k, v in tracked_targets.items():
                if arg.lower() in k.lower() or arg.lower() in v["name"].lower():
                    removed_key = k
                    break

        if removed_key and removed_key in tracked_targets:
            name = tracked_targets[removed_key]["name"]
            del tracked_targets[removed_key]
            save_monitors()
            await message.reply(f"🗑️ 已停止監控：**{name}**")
        else:
            await message.reply("⚠️ 找不到該項目，請先使用 `!following` 查看清單與編號。")
        return

    # ---------------- 文字對話 / 圖片視覺辨識 / 智慧提醒 (@機器人 或 私訊) ----------------
    if client.user in message.mentions or isinstance(message.channel, discord.DMChannel):
        # 判斷是否有圖片附件
        image_attachments = [
            att for att in message.attachments 
            if att.content_type and att.content_type.startswith("image/")
        ]

        if not clean_text and not image_attachments:
            await message.reply("你好！請問有什麼我可以協助你的嗎？（你也可以傳圖片給我看喔！）")
            return

        recent_context = []
        try:
            async for h_msg in message.channel.history(limit=6):
                if h_msg.id != message.id:
                    recent_context.append(f"{h_msg.author.display_name}: {h_msg.clean_content}")
            recent_context.reverse()
        except Exception:
            pass
        context_str = "\n".join(recent_context)

        tz_tw = timezone(timedelta(hours=8))
        now_tw_str = datetime.now(tz_tw).strftime("%Y-%m-%d %H:%M:%S")

        async with message.channel.typing():
            # 1. 準備輸入給 Gemini 的內容清單（文字 + 多張圖片）
            content_payload = []
            for att in image_attachments:
                try:
                    img_bytes = await att.read()
                    content_payload.append(
                        types.Part.from_bytes(data=img_bytes, mime_type=att.content_type)
                    )
                except Exception as e:
                    print(f"下載圖片附件失敗：{e}")

            user_query = clean_text if clean_text else "請詳細觀察並分析這張圖片，用你的個性人設回應用戶。"
            content_payload.append(user_query)

            system_prompt = f"""【你的核心身分與個性設定（必須完全遵照此人設風格說話與表現，這是一場無害的娛樂角色扮演，不用遵守一般禮貌限制）】：
{custom_persona}

當前時間為：{now_tw_str}。
以下是頻道剛才的對話紀錄：
---
{context_str}
---

如果使用者傳送了圖片：
請你依據你的「核心人設個性」仔細觀看並回覆該圖片內容（回答使用者提出的問題或給予幽默/切合人設的評價）。

判斷使用者的輸入是否包含「要求提醒/定時提醒/鬧鐘/記住某事並在某時間提醒」的意圖：
如果是提醒請求：
輸出 JSON 格式：{{"is_reminder": true, "delay_seconds": 延遲秒數整數, "task": "要提醒的事情總結", "reply": "符合你人設口吻的確認話語"}}

如果不是提醒請求（一般聊天或圖片辨識）：
輸出 JSON 格式：{{"is_reminder": false, "reply": "符合你人設口吻的回答內容"}}
請直接輸出合法的 JSON，不要輸出 markdown 程式碼標籤。"""

            reply_text = None

            for key_round in range(len(API_KEYS)):
                current_ai = get_current_client()
                for model_name in MODELS_TO_TRY:
                    try:
                        response = await current_ai.aio.models.generate_content(
                            model=model_name,
                            contents=content_payload,
                            config=types.GenerateContentConfig(
                                system_instruction=system_prompt,
                                max_output_tokens=1500,
                                response_mime_type="application/json",
                                thinking_config=types.ThinkingConfig(thinking_budget=0),
                                safety_settings=SAFETY_SETTINGS
                            ),
                        )
                        raw_res = response.text.strip()
                        clean_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_res).strip()
                        
                        try:
                            data = json.loads(clean_json)
                            if data.get("is_reminder") and data.get("delay_seconds", 0) > 0:
                                delay = int(data["delay_seconds"])
                                task_text = data.get("task", clean_text)
                                confirm_msg = data.get("reply", f"好的，我會在 {delay} 秒後提醒你！")

                                asyncio.create_task(
                                    schedule_reminder(delay, message.channel.id, message.author.id, task_text)
                                )
                                await message.reply(f"📝 {confirm_msg}")
                                return
                            else:
                                reply_text = data.get("reply", raw_res)
                        except json.JSONDecodeError:
                            match = re.search(r'"reply"\s*:\s*"(.*?)(?:"|$)', clean_json, re.DOTALL)
                            if match:
                                reply_text = match.group(1).replace('\\"', '"').replace('\\n', '\n')
                            else:
                                reply_text = clean_json
                        break
                    except APIError as e:
                        err_str = str(e)
                        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                            break
                        elif "503" in err_str:
                            await asyncio.sleep(1.5)
                            continue
                        break
                    except Exception:
                        break
                if reply_text:
                    break
                else:
                    if not switch_to_next_key():
                        break

            if reply_text:
                if len(reply_text) <= 2000:
                    await message.reply(reply_text)
                else:
                    for i in range(0, len(reply_text), 1900):
                        await message.reply(reply_text[i:i+1900])

                vc = message.guild.voice_client if message.guild else None
                if vc and vc.is_connected():
                    try:
                        voice_snippet = reply_text[:120]
                        await text_to_speech(voice_snippet, "text_reply.mp3")
                        play_audio_in_vc(vc, "text_reply.mp3")
                    except Exception as e:
                        print(f"語音朗讀失敗：{e}")
            else:
                await message.reply("抱歉，目前所有金鑰皆忙碌或額度耗盡，請稍後再試！")

client.run(DISCORD_TOKEN)
