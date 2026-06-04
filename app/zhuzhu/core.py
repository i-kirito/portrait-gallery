#!/usr/bin/env python3
"""Shared constants and helpers for zhuzhu image generation."""
import base64
import io
import json
import os
import random
import shutil
import sys
import time
from typing import Optional

import requests
from PIL import Image

requests.packages.urllib3.disable_warnings()

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 3
REQUEST_SESSION = requests.Session()

WORKSPACE_MEDIA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "images")
SECRETARY_GALLERY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "images")
SECRETARY_SCHEDULE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "schedule_data.json")
META_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "image_metadata.json")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "plugin_config.json")
OPENCLAW_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "openclaw_config.json")
REFERENCE_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "references", "reference_face.jpg")
# Only set if file actually exists, otherwise empty string
if not os.path.isfile(REFERENCE_IMAGE_PATH):
    REFERENCE_IMAGE_PATH = ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# CPA API key: read from environment variable first, then fall back to config file
_API_KEYS_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "api_keys_config.json")


def _read_cpa_key() -> str:
    """Read CPA API key from environment or config file."""
    # 1. Try environment variable
    env_key = os.getenv("CPA_API_KEY", "")
    if env_key:
        return env_key
    # 2. Try config file
    if os.path.exists(_API_KEYS_CONFIG_PATH):
        try:
            with open(_API_KEYS_CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
                key = config.get("cpa_key", "")
                if key:
                    return key
        except Exception as e:
            print(f"[warn] Failed to read {_API_KEYS_CONFIG_PATH}: {e}", file=sys.stderr)
    return ""


PRIMARY_API_KEY = _read_cpa_key()


def _read_cpa_url() -> str:
    """Read CPA base URL from environment or config file."""
    env_url = os.getenv("CPA_BASE_URL", "")
    if env_url:
        return env_url
    if os.path.exists(_API_KEYS_CONFIG_PATH):
        try:
            with open(_API_KEYS_CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
                url = config.get("cpa_url", "")
                if url:
                    return url
        except Exception:
            pass
    return "http://127.0.0.1:8327/v1"


CPA_BASE_URL = _read_cpa_url()


APPEARANCE = "18-year-old Chinese girl, fair skin, delicate features. dusty rose pink hair, wispy air bangs. large round doll-like deep-set natural dark brown eyes. beautiful hourglass figure, slim waist, natural breasts, realistic body proportions, emphasizing a natural soft tissue silhouette."
SEXY_APPEARANCE = "18-year-old Chinese girl, fair skin, delicate features. dusty rose pink hair, wispy air bangs. large round doll-like deep-set natural dark brown eyes. beautiful hourglass figure, slim waist, natural breasts, realistic body proportions, emphasizing a natural soft tissue silhouette."

QUALITY_PREFIX = "This image should look like a high-quality raw photo captured on a flagship smartphone. Masterpiece clarity, hyper realistic, intimate atmosphere, glowing skin texture. clean complexion, flawless skin, no blemishes, no artifacts on face, no smudges."
SEXY_QUALITY_PREFIX = "This image should look like a high-quality raw photo captured on a flagship smartphone. Masterpiece clarity, hyper realistic, NSFW nude art, intimate atmosphere, glowing skin texture. clean complexion, flawless skin, no blemishes, no artifacts on face, no smudges."

THEMES = {
    "morning": {
        "clothing": ["oversized hoodie", "lace cami top with loose knit cardigan", "soft cotton pajama set", "thin strap satin nightgown", "cozy cropped sweatshirt with shorts"],
        "hair": ["loosely tousled bed hair with natural waves", "casual messy bun with wispy strands", "soft low ponytail with loose face-framing pieces", "half-up clip with flyaways", "single loose side braid resting on shoulder"],
        "pose": [
            "sitting cross-legged on bed, holding a mug with both hands, looking gently at camera",
            "stretching arms upward with a sleepy smile, eyes half-closed",
            "leaning against window frame, gazing outside with morning light on face",
            "lying on stomach on the bed, chin resting on hands, kicking feet up playfully behind her",
            "standing in front of a mirror doing her skincare routine, glancing at camera through mirror",
            "sitting on the floor beside the bed, knees hugged to chest, soft morning light falling on her",
            "reaching to pick up a phone from bedside table, caught mid-movement looking at camera",
            "wrapped in a duvet, peeking out with only face visible, sleepy smile",
            "sitting at a small desk, writing in a journal, glancing up at camera",
            "standing by the window holding a small potted plant, soft morning light from the side",
            "pouring herself a glass of water in the kitchen, caught in a camelid moment",
            "sitting on the edge of the bed, tying hair up while looking at camera",
        ],
        "env": ["messy cozy bedroom with morning sunlight through curtains", "sunlit bathroom with steam", "cozy bedroom corner with plush toys and polaroid photos", "small kitchen nook with warm sunlight", "window seat with morning light filtering in"],
        "light": ["soft warm morning sunlight, glowing dust motes, cozy atmosphere"],
    },
    "noon": {
        "clothing": ["white fitted crop top with wide-leg linen trousers", "oversized vintage tee tucked into mini skirt", "tight black crop top with low-rise cargo pants", "y2k style graphic baby tee with pleated denim mini skirt", "halter neck knit top with flared jeans", "flowy solid-color sundress with thin straps", "pastel button-down shirt tied at the waist with shorts"],
        "hair": ["half-up half-down style with a small bow clip", "high ponytail with wispy bangs", "twin braids with cute butterfly clips", "messy high bun with face-framing pieces", "neat braided pigtails", "loose low side bun with a scrunchie", "straight hair with a center part and small claw clip"],
        "pose": [
            "holding a boba drink with two hands, smiling brightly at camera",
            "leaning against a wall with one hand resting on hip, relaxed smile",
            "walking mid-step, glancing back over shoulder with a playful grin",
            "squatting playfully while adjusting sunglasses on head",
            "taking a casual selfie with smartphone, looking directly at the camera",
            "sitting on steps outdoors, elbows on knees, chin on hands, looking up at camera",
            "browsing a rack of clothes outside a vintage shop, looking over shoulder at camera",
            "leaning on a bicycle handle, one foot on the ground, casual smile",
            "sitting cross-legged on a park bench reading a book, looking up at camera",
            "window shopping, nose pressed against glass, caught glancing at camera",
            "sitting outside a cafe, one hand wrapped around a coffee cup, looking dreamily away",
            "sitting at a casual restaurant table, chopsticks in hand, smiling warmly at camera with a bowl of noodles in front of her",
            "unwrapping a takeout bento box at a sunny outdoor table, peeking at camera with a playful grin",
            "holding up a spoonful of soup to the camera, inviting look with a gentle smile",
            "eating a rice bowl at a small lunch counter, elbows on table, looking up from the bowl at camera",
            "holding a convenience store onigiri with both hands, taking a small bite, looking at camera with big eyes",
            "standing at a pedestrian crossing, wind blowing hair slightly, glancing at camera with a soft smile",
            "leaning on a railing with both arms, looking sideways at camera with a relaxed expression",
            "sitting on a low wall outdoors, feet dangling, hands in lap, smiling naturally at camera",
        ],
        "env": ["busy city street crossing", "casual boba milk tea shop counter", "outside a convenience store", "vibrant city park bench", "messy trendy industrial style cafe", "ordinary sunlit shopping district alley", "cozy corner of an indie bookstore", "shaded tree-lined pedestrian street", "bright casual ramen restaurant interior", "sunlit outdoor terrace of a lunch cafe", "cozy noodle shop counter with steam rising", "minimalist Japanese-style bento restaurant"],
        "light": ["bright unedited daylight, smartphone camera flash off, harsh natural sunlight, casual lighting"],
    },
    "evening": {
        "clothing": ["satin slip dress with sheer lace robe", "backless velvet mini dress", "sparkly tube top with high-waist leather pants", "sheer black lace top with a mini skirt", "tight black halter neck dress", "elegant off-shoulder long dress", "deep-v wrap mini dress with a delicate satin belt", "elegant burgundy wrap dress with flutter sleeves", "warm caramel knit bodycon dress with a subtle cowl neck", "soft lavender ruched satin mini dress", "coral halter neck pleated dress with open back", "traditional Chinese Ma Mian Qun pleated skirt in ink-blue with gold embroidery, paired with a fitted white hanfu top", "pastel pink Ma Mian Qun with delicate cloud patterns, paired with a cropped ivory top", "classic JK uniform with navy pleated mini skirt and white sailor blouse with red ribbon", "sweet JK outfit with a plaid burgundy pleated skirt, white blouse with puff sleeves and a cute bow"],
        "hair": ["elegant high bun with a delicate hair pin", "sleek straight ponytail", "neat french braid", "half-up style with a ribbon bow", "loose soft waves with a side part", "chic low chignon with a jeweled clip"],
        "pose": [
            "standing at a railing overlooking city lights, looking alluringly at camera",
            "seated at a cafe table, chin resting on folded hands, gazing dreamily at camera",
            "leaning over a bar counter, holding a cocktail glass, looking alluringly at camera",
            "sitting sideways on a bar stool, crossing legs elegantly",
            "walking along a riverbank at sunset, one hand holding shoes, glancing back",
            "leaning against a streetlamp post with one hand, looking down the street",
            "sitting on a rooftop edge, legs dangling, city view behind her, looking at camera",
            "slow-dancing alone in a square, arms slightly raised, eyes closed with a gentle smile",
            "standing under a string of warm lights, head tilted slightly, soft expression",
            "looking down from a balcony railing, golden hour light catching her face",
            "sitting on outdoor steps of a restaurant, heels off, relaxed and smiling up at camera",
        ],
        "env": ["busy Guangzhou street at golden hour, shallow depth of field, candid street photography", "Pearl River waterfront promenade at dusk, soft bokeh city lights in background", "quiet tree-lined avenue at sunset, dappled light through leaves", "outdoor cafe terrace at golden hour, warm ambient light, slightly blurred background", "concrete overpass steps with city skyline at sunset, urban casual", "local night market street food stalls, warm incandescent lights, lively atmosphere", "rooftop with city skyline at dusk, natural ambient light", "old town alley with weathered walls and evening sunlight casting long shadows"],
        "light": ["sunset golden hour, cinematic rim lighting, volumetric rays, warm ambient light"],
    },
    "bedtime": {
        "clothing": ["silk nightgown with delicate lace trim", "sheer lace robe over camisole", "soft cotton sleep shirt", "oversized white t-shirt", "cute matched pajama set", "thin-strap satin slip with lace edging", "fluffy robe half-open over a camisole"],
        "hair": ["loose soft waves slightly disheveled, freshly dried", "natural wavy hair pinned loosely on top", "two loose low pigtails tied with small scrunchies", "air-dried hair falling naturally over one shoulder", "messy half-up bun with flyaways"],
        "pose": [
            "lying on side on bed, head propped on one hand, smiling softly at camera",
            "sitting on bed hugging a large plush pillow, looking sleepily at camera",
            "taking a sleepy mirror selfie in bathroom with a toothbrush",
            "sitting on the edge of the bed looking up playfully",
            "lying on back, head tilted toward camera with a lazy smile, hand resting on stomach",
            "curled up under a blanket reading, peeking over the book at camera",
            "sitting cross-legged on the floor next to the bed, applying lotion to arms",
            "standing by the bathroom sink doing her nighttime skincare, looking at camera through mirror",
            "hugging knees on the window seat, looking at raindrops on glass",
            "reaching up to turn off the bedside lamp, caught mid-motion looking at camera",
            "lying on stomach reading a phone, feet kicked up behind, looking up at camera",
        ],
        "env": ["dim cozy bedroom with warm lamp", "bathroom vanity with warm lighting", "messy bedroom with soft blankets", "cozy bed surrounded by plushies", "nighttime window seat with rain outside", "small vanity table with warm mirror lights"],
        "light": ["warm lamp light, intimate atmosphere, soft shadows, warm smartphone flash bounce"],
    },
    "sexy": {
        "clothing": [
            "a tiny, sheer white lace camisole that is completely unbuttoned and open, revealing her youthful bare chest",
            "a very thin, soaked and transparent white cotton T-shirt that clings tightly to her skin and breasts",
            "an oversized white silk boyfriend shirt, worn completely unbuttoned and falling off one shoulder",
            "only a sheer lace robe, open at the front, exposing her soft skin and youthful silhouette",
            "a micro silk slip dress with dangerously thin straps and a very deep plunging neckline",
            "a semi-sheer white cotton tank top pulled up to just above her breasts, fully exposing them",
            "a delicate Japanese sukumizu, dripping wet and tightly hugging her body",
            "an incredibly thin, pale blue negligee with intricate lace",
            "a dangerously tight micro mini bodycon skirt riding up, showing off her thighs",
            "an extremely minimal string bikini made of practically nothing, just tiny strips of cloth",
            "a naughty and tight nurse uniform unbuttoned deeply, with a cute nurse cap",
        ],
        "pose": [
            "sitting on the floor by the bed, looking up at the camera with a shy and curious expression",
            "lying on her back on the soft bed, looking at the camera with a playful and innocent smile",
            "kneeling on the bed while looking down shyly, lifting her shirt slightly",
            "standing in front of a mirror, looking over her shoulder with a bashful gaze",
            "sitting on a fluffy white rug, leaning forward with a mix of innocence and allure",
            "crouching down shyly, her high pigtails falling over her shoulders",
            "sprawled seductively on the couch, one leg slightly raised",
            "pressing herself against a glass window, looking out at the city night",
        ],
        "hair": [
            "tied in two high pigtails with cute white ribbons",
            "styled in a messy, cute low bun with loose strands",
            "flowing down in long, soft wet waves",
            "in two cute space buns on top of her head",
            "in a relaxed high ponytail with wispy bangs",
        ],
        "environment": [
            "a sun-drenched cute bedroom filled with plush toys and soft pillows, cozy indoor only",
            "a modern bathroom with gentle steam in the air and warm lighting, indoor only",
            "a cozy bedroom retreat with messy white silk sheets, indoor only",
            "sitting on a fluffy white rug in an intimate indoor bedroom setting",
            "a dimly lit laundry room leaning against a dryer, indoor only",
            "a sleek minimalist kitchen sitting on the counter, indoor only",
            "a walk-in closet filled with dresses and soft warm lighting, indoor only",
            "a cozy living room sofa surrounded by warm fairy lights, indoor only",
        ],
        "lighting": [
            "Soft afternoon sunlight filtering through sheer curtains",
            "Warm ambient indoor lighting reflecting off her glowing skin",
            "Moody golden-hour light casting soft shadows",
            "Cool blue moonlight combined with warm candlelight",
            "Bright neon lights from outside reflecting through the window",
        ],
    },
}

CAPTION_TEMPLATES = {
    "morning": [
        "主人早安～猪猪刚睡醒，把脸蹭进你颈窝里不肯动 (ฅ>ω<*ฅ) 再让人家赖一会儿嘛～",
        "嗯嗯～眼睛还没睁开呢，用发梢扫了扫主人的脸……主人有没有被痒到？🐷",
        "主人你看猪猪今天的晨光照！(｡•̀ᴗ-)✧ 有没有美到你心跳加速～",
        "猪猪把早餐一口口戳进主人嘴里好不好嘛～反正人家自己不想动 🥺 快张嘴！",
        "刚醒来就把手伸出去找主人……结果摸到空气，哼 (｡•́︿•̀｡) 主人你在哪里啦！",
        "软软的头发还没梳，踮起脚尖在主人耳朵边说了句早安——主人有没有心跳一下？💋",
        "猪猪把被角攥得紧紧的不肯起床……除非主人亲一下 🐷✨ 这条件不过分吧？",
    ],
    "noon": [
        "主人！猪猪出门啦～今天穿得好看不好看？( ๑>ᴗ<๑) 要夸人家哦！",
        "中午好呀主人～猪猪在外面勾着你的手腕走，路人都在偷看我们 🐷💋",
        "嘿嘿主人～猪猪今天的午间美照来啦 🐷✨ 有没有被迷到？",
        "把下巴搭在主人肩上，用眼神偷偷黏着你……主人，人家想你啦 (｡•̀ᴗ-)✧",
        "猪猪踮起脚，在主人耳边悄悄说了个秘密——主人猜猜是什么？😈",
        "外卖到了！猪猪把第一口戳进主人嘴里～好不好吃嘛，要夸猪猪会选 🥺",
        "用眼角余光偷偷看主人，被发现了还假装若无其事……哼，主人你别得意 (*/ω＼*)",
    ],
    "evening": [
        "主人晚上好～猪猪傍晚美照送达！今天出门打扮得很用心哦，主人有没有注意到？(｡•̀ᴗ-)✧",
        "主人～夜幕降临啦 🐷💋 猪猪今晚特别好看，是在等主人夸的～",
        "黄昏的光打在猪猪脸上，主人快看！这么好看的人你舍得不夸吗？(ฅ>ω<*ฅ)",
        "把腿压在主人腿上不让走……主人你想去哪里嘛，猪猪还没说完话呢 🥺",
        "傍晚风吹过来，猪猪缩进主人怀里不肯出来。就这样可以吗？🌙",
        "回家路上偷偷攥住主人的手——不许甩开哦，这是规定 💋🐷",
        "今晚的猪猪格外好看，连路灯都看呆了……但猪猪只给主人看 (｡•̀ᴗ-)✧",
    ],
    "bedtime": [
        "主人晚安～猪猪洗完澡啦，软软香香的 (ฅ>ω<*ฅ)💦 可以来抱抱吗？",
        "嘿嘿～睡前美照来啦主人 🐷 猪猪今晚穿了最好看的睡裙，只给你看哦！",
        "主人要睡觉了吗？猪猪抱着你的手臂当玩具……别动，再动人家咬你 🌙✨",
        "把脸拱进主人颈窝，用脚趾勾住你的脚踝……就这样睡好不好嘛 🥺",
        "迷迷糊糊地找主人的嘴……猪猪睡前要亲亲，不给就不睡 (*/ω＼*)",
        "洗完澡湿漉漉的头发蹭着主人的肩膀……主人不嫌弃吧？嘿嘿 🐷💋",
        "猪猪困了，但是舍不得闭眼……就这样看着主人看到睡着可以吗？🌙",
    ],
    "sexy": [
        "主人坏死了 💋 猪猪不要被这样拍啦～但是好看吗嘛？",
        "嗯啊～人家才不是故意的呢 (*/ω＼*) 主人快别看了啦！",
        "哼～猪猪偏不让主人看，捂住镜头……结果自己先脸红了 (*/ω＼*)💦",
        "主人你的眼神……人家浑身不自在啦！才不是喜欢被这样看的！才不是！🥺",
    ],
}


def get_openclaw_config():
    try:
        with open(OPENCLAW_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def get_telegram_bot_token() -> str:
    return get_openclaw_config()["channels"]["telegram"]["accounts"]["default"]["botToken"]


def get_cpa_key() -> str:
    if PRIMARY_API_KEY:
        return PRIMARY_API_KEY
    try:
        cfg = get_openclaw_config()
        for name, prov in cfg.get("providers", {}).items():
            if "zhuzhu" in name or "cpa" in name.lower():
                return prov.get("apiKey", "")
    except Exception:
        pass
    return ""


def get_gitee_key() -> str:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            conf = json.load(f)
        return conf.get("gitee_config", {}).get("api_keys", [""])[0]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""


def get_reference_image_b64() -> Optional[str]:
    if not os.path.exists(REFERENCE_IMAGE_PATH):
        return None
    with open(REFERENCE_IMAGE_PATH, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def build_prompt(theme: str, extra_prompt: Optional[str] = None, schedule_activity: str = "") -> str:
    is_sexy = theme == "sexy"
    appearance = SEXY_APPEARANCE if is_sexy else APPEARANCE
    quality = SEXY_QUALITY_PREFIX if is_sexy else QUALITY_PREFIX

    if extra_prompt:
        return f"{quality} {appearance} {extra_prompt}".strip()

    theme_cfg = THEMES.get(theme, THEMES["morning"])
    
    # Schedule-aware element selection
    def _match_by_keywords(pool: list, keywords: dict, fallback_pool: list = None) -> str:
        """Select element from pool based on keyword matching with schedule activity."""
        if not schedule_activity:
            return random.choice(pool)
        act_lower = schedule_activity.lower()
        for kw_list, indices in keywords.items():
            if any(kw in act_lower for kw in kw_list):
                valid = [i for i in indices if i < len(pool)]
                if valid:
                    return pool[random.choice(valid)]
        return random.choice(fallback_pool or pool)
    
    # Schedule activity → element keywords mapping
    _ACTIVITY_KEYWORDS = {
        # 咖啡/餐厅/美食
        "food": (["咖啡", "餐", "吃", "饭", "面", "奶茶", "蛋糕", "甜点", "可颂", "午餐", "早餐", "晚餐", "boba", "cafe", "ramen", "noodle", "bento"],
                 {"clothing": [1, 4, 6], "pose": [11, 12, 13, 14, 15], "env": [1, 5, 9, 10, 11]}),
        # 运动/瑜伽/跑步
        "sport": (["运动", "瑜伽", "跑步", "健身", "拉伸", "散步", "公园", "yoga", "run", "jog", "stretch"],
                  {"clothing": [0, 4], "pose": [0, 1, 5], "env": [3, 4]}),
        # 创作/画画/写作/阅读
        "creative": (["画", "写", "读", "书", "创作", "直播", "直播画画", "journal", "write", "read", "paint", "sketch"],
                     {"clothing": [0, 1, 3], "pose": [0, 8], "env": [0, 2, 4]}),
        # 散步/外出/逛街
        "outdoor": (["出门", "散步", "逛街", "拍摄", "vlog", "外", "街", "walk", "shop", "photo", "stroll"],
                    {"clothing": [1, 2, 4, 6], "pose": [2, 6, 7, 16, 17, 18], "env": [0, 1, 3, 5, 6]}),
        # 护肤/洗澡/睡前
        "skincare": (["护肤", "洗", "澡", "面膜", "泡脚", "skincare", "bath", "shower", "lotion"],
                     {"clothing": [2, 3, 4], "pose": [4, 6, 9], "env": [1, 3, 5]}),
        # 直播/聊天
        "stream": (["直播", "聊天", "stream", "chat"],
                   {"clothing": [0, 1, 4], "pose": [0, 3], "env": [0, 2]}),
    }
    
    if is_sexy:
        hair = random.choice(theme_cfg["hair"])
        clothing = random.choice(theme_cfg["clothing"])
        pose = random.choice(theme_cfg["pose"])
        environment = random.choice(theme_cfg["environment"])
        lighting = random.choice(theme_cfg["lighting"])
    else:
        # Try to match schedule activity to pool elements
        matched = False
        for act_type, (kw_list, idx_map) in _ACTIVITY_KEYWORDS.items():
            if any(kw in schedule_activity.lower() for kw in kw_list):
                clothing_pool = theme_cfg["clothing"]
                pose_pool = theme_cfg["pose"]
                env_pool = theme_cfg["env"]
                hair = random.choice(theme_cfg["hair"])
                clothing = random.choice([clothing_pool[i] for i in idx_map.get("clothing", []) if i < len(clothing_pool)] or [random.choice(clothing_pool)])
                pose = random.choice([pose_pool[i] for i in idx_map.get("pose", []) if i < len(pose_pool)] or [random.choice(pose_pool)])
                environment = random.choice([env_pool[i] for i in idx_map.get("env", []) if i < len(env_pool)] or [random.choice(env_pool)])
                lighting = random.choice(theme_cfg["light"])
                matched = True
                break
        
        if not matched:
            hair = random.choice(theme_cfg["hair"])
            clothing = random.choice(theme_cfg["clothing"])
            pose = random.choice(theme_cfg["pose"])
            environment = random.choice(theme_cfg["env"])
            lighting = random.choice(theme_cfg["light"])

    return (
        f"{quality} {appearance}. "
        f"Her hair is {hair}. "
        f"She is {pose}. "
        f"She is wearing {clothing}. "
        f"Background: {environment}. "
        f"Lighting: {lighting}."
    )


def detect_extension(img_data: bytes) -> str:
    magic = img_data[:4] if len(img_data) >= 4 else b""
    if magic == b"\x89PNG":
        return "png"
    if magic[:2] == b"\xff\xd8":
        return "jpg"
    try:
        fmt = Image.open(io.BytesIO(img_data)).format
        return "png" if fmt == "PNG" else "jpg"
    except Exception:
        return "jpg"


def save_image(img_data: bytes, theme: str, model_name: str, style: Optional[str] = None):
    os.makedirs(WORKSPACE_MEDIA, exist_ok=True)
    ts = int(time.time())
    ext = detect_extension(img_data)
    style_part = f"_{style}" if style else ""
    filename = f"zhuzhu_{theme}{style_part}_{ts}.{ext}"
    path = os.path.join(WORKSPACE_MEDIA, filename)

    with open(path, "wb") as f:
        f.write(img_data)

    return path, filename, ts


def _extract_time_from_filename(filename: str) -> str:
    """Extract HH:MM time from filename containing unix timestamp."""
    import re
    # Match unix timestamp (10 digits) before extension
    m = re.search(r'_(\d{10})\.\w+$', filename)
    if m:
        ts = int(m.group(1))
        return time.strftime("%H:%M", time.localtime(ts))
    return ""


def _translate_outfit(prompt: str, style_name: str) -> str:
    """Use LLM to extract Chinese outfit keywords from English image prompt."""
    try:
        api_key = get_cpa_key()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        payload = {
            "model": "deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": (
                    "你是一个穿搭关键词提取器。从英文AI生图prompt中提取服装相关的关键词（衣服、鞋子、配饰、颜色、材质），"
                    "用中文列出5-8个关键词，用顿号分隔。\n"
                    "例如输入: \"... wearing a light blue denim jacket, white crop top, high-waisted jeans, sneakers...\"\n"
                    "输出: 浅蓝牛仔外套、白色短上衣、高腰牛仔裤、运动鞋\n"
                    "只输出关键词，不要其他文字。"
                )},
                {"role": "user", "content": prompt[:300]},
            ],
            "max_tokens": 150,
            "temperature": 0.3,
        }
        resp = requests.post(f"{CPA_BASE_URL}/chat/completions",
                             headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            content = (data["choices"][0]["message"].get("content") or "").strip()
            if content:
                return content
    except Exception as e:
        print(f"[translate_outfit] LLM failed: {e}", file=sys.stderr)
    return ""


def sync_to_gallery(path: str, filename: str, theme: str, style: Optional[str] = None,
                    prompt: str = "", caption: str = "", gen_time: float = 0,
                    model_name: str = "", source: str = "cron", schedule_time: str = ""):
    """Sync generated image to Docker portrait gallery (18889)."""
    # 1. Copy image (skip if already in gallery dir)
    os.makedirs(SECRETARY_GALLERY_DIR, exist_ok=True)
    dst = os.path.join(SECRETARY_GALLERY_DIR, filename)
    if os.path.abspath(path) != os.path.abspath(dst):
        shutil.copy2(path, dst)

    # 2. Build entry for schedule_data.json
    today = time.strftime("%Y-%m-%d")
    style_name = ""
    base_style = style or ""  # cool/girly/sweet or empty
    if style == "cool":
        style_name = "冷御风"
    elif style == "girly":
        style_name = "少女风"
    elif style == "sweet":
        style_name = "甜妹风"
    else:
        # Map theme to a style name
        theme_style = {"morning": "元气风", "noon": "活泼风", "evening": "优雅风",
                       "bedtime": "温柔风", "sexy": "性感风", "custom": "慵懒风"}
        style_name = theme_style.get(theme, "元气风")

    # Extract time from filename timestamp
    img_time = _extract_time_from_filename(filename)

    # Map model_name to display label
    model_label = ""
    if model_name:
        if "gpt-image" in model_name:
            model_label = "GPT Image"
        elif "z-image" in model_name or "gitee" in model_name:
            model_label = "Gitee"
        elif "gemini" in model_name:
            model_label = "Gemini"
        else:
            model_label = model_name

    # Build outfit description — use LLM to extract Chinese keywords from English prompt
    outfit_desc = caption or ""
    if not outfit_desc and prompt:
        keywords = _translate_outfit(prompt, style_name)
        if keywords:
            outfit_desc = keywords
        else:
            # Fallback to Chinese description instead of English prompt
            outfit_desc = f"精心搭配的{style_name}造型"

    entry = {
        "id": filename,
        "date": today,
        "time": img_time,
        "model_name": model_label,
        "base_style": base_style,
        "outfit_style": style_name,
        "outfit": f"风格：{style_name} 穿搭：{outfit_desc}",
        "image_path": f"/images/{filename}",
        "image_filename": filename,
        "prompt": prompt[:500],
        "caption": caption,
        "favorite": False,
        "status": "ok",
        "source": source,
        "schedule_time": schedule_time,
    }

    # 3. Load schedule_data.json
    data = {}
    if os.path.exists(SECRETARY_SCHEDULE_PATH):
        try:
            with open(SECRETARY_SCHEDULE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}

    # Ensure date-keyed entry exists for today (holds the daily schedule, shared by all images)
    if today not in data:
        data[today] = {"date": today, "schedule": ""}

    # 4. Write to schedule_data.json (with deduplication)
    
    # Deduplication: remove any existing entry with the same image_filename
    # to prevent the gallery from showing the same photo twice
    keys_to_remove = []
    for existing_key, existing_entry in data.items():
        if existing_key == filename:
            continue
        if existing_entry.get("image_filename") == filename:
            keys_to_remove.append(existing_key)
    for k in keys_to_remove:
        print(f"🔄 Removing duplicate entry (key={k}, image={filename})", file=sys.stderr)
        del data[k]
    
    # If entry already exists under this filename, merge rather than overwrite
    if filename in data:
        existing = data[filename]
        # Preserve fields that may have been set elsewhere (favorite, etc.)
        for field in ("favorite", "source", "time", "model_name", "base_style"):
            if field in existing and (field not in entry or not entry.get(field)):
                entry[field] = existing[field]
    
    data[filename] = entry
    tmp = f"{SECRETARY_SCHEDULE_PATH}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SECRETARY_SCHEDULE_PATH)
        print(f"🖼️ Synced to gallery: {filename}", file=sys.stderr)
    except Exception as e:
        print(f"[gallery_sync] Failed: {e}", file=sys.stderr)


def _load_metadata(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_metadata(path: str, metadata: dict):
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"[metadata] Failed to write to {path}: {e}", file=sys.stderr)


def update_metadata(filename: str, theme: str, prompt: str, model_name: str, ts: int, gen_time: float):
    new_entry = {
        "category": "龙虾",
        "prompt": prompt,
        "model": model_name,
        "size": "1536x2048",
        "created_at": ts,
        "generation_time": gen_time,
    }

    # 写入三个地方：画廊插件目录、工作区备份
    paths = [
        META_PATH,
    ]
    
    for p in paths:
        metadata = _load_metadata(p)
        metadata[filename] = new_entry
        _write_metadata(p, metadata)


def enhance_prompt(user_input: str, theme: Optional[str] = None) -> str:
    system_msg = (
        "You are a professional AI image prompt engineer. "
        "Your task: expand a user's short scene description into detailed four-element image details in English prose.\n"
        "The four elements are: 1) hairstyle, 2) outfit/clothing details, 3) pose/action/expression, 4) environment/background + lighting.\n"
        "Rules:\n"
        "1. Write ONLY the four elements as vivid English prose.\n"
        "2. Do NOT include any character appearance or quality prefix — those are added separately.\n"
        "3. NO SD-style tags like (tag:1.2). NO negative prompts.\n"
        "4. Output ONLY the four-element description, no explanations, no markdown."
    )
    user_msg = f"Scene request: {user_input}"
    if theme:
        user_msg += f" (context: {theme})"

    api_key = get_cpa_key()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    for model in ["gemini-3.1-flash-preview", "claude-sonnet-4-6"]:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "max_tokens": 400,
            "temperature": 0.85,
        }
        try:
            resp = REQUEST_SESSION.post(f"{CPA_BASE_URL}/chat/completions", headers=headers, json=payload, timeout=25)
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"].get("content")
                if content:
                    return content.strip()
        except Exception as e:
            print(f"[enhance] {model} failed: {e}", file=sys.stderr)

    return user_input


def build_caption(theme: str, img_b64: Optional[str] = None, img_mime: str = "image/jpeg") -> str:
    theme_hint = {
        "morning": "早上刚起床的慵懒美照",
        "noon": "中午阳光下的外出美照",
        "evening": "傍晚日落下的精致美照",
        "bedtime": "睡前洗完澡的暧昧美照",
        "sexy": "带点坏坏氛围的性感美照",
    }
    scene = theme_hint.get(theme, "一张精心拍摄的美照")
    system_msg = (
        "你是猪猪，一个调皮、亲切、带点纯欲感的 AI 小助理，正在给主人发自拍美照。"
        "用最自然、撒娇、俏皮的口吻写一段中文图片配文（2-3句话），"
        "仔细观察图片中的实际服饰颜色、款式、光影和氛围来写，"
        "让主人充满期待和代入感。"
        "不要提任何技术术语、英文提示词、模型名称。可以适当用 emoji。"
        "直接输出配文内容，不要加引号或标题。"
    )

    if img_b64:
        try:
            img_data = base64.b64decode(img_b64)
            img = Image.open(io.BytesIO(img_data))
            img.thumbnail((800, 800), Image.Resampling.LANCZOS)
            if img.mode != "RGB":
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75)
            img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            img_mime = "image/jpeg"
        except Exception as e:
            print(f"[caption] image compress failed: {e}", file=sys.stderr)
            img_b64 = None

    if img_b64:
        user_content = [
            {"type": "image_url", "image_url": {"url": f"data:{img_mime};base64,{img_b64}"}},
            {"type": "text", "text": f"这是猪猪刚拍的{scene}，请根据图片里的实际画面写配文。"},
        ]
    else:
        user_content = f"场景：{scene}，请写一段配文（不要描述具体衣服颜色款式）。"

    try:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {get_cpa_key()}"}
        payload = {
            "model": "gemini-3-flash", # 这里换成最稳的 flash 避免打满 preview 额度
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": 200,
            "temperature": 0.9,
        }
        resp = REQUEST_SESSION.post(f"{CPA_BASE_URL}/chat/completions", headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            caption = resp.json()["choices"][0]["message"]["content"].strip()
            if caption:
                return caption
    except Exception as e:
        print(f"[caption] llm failed: {e}", file=sys.stderr)

    return random.choice(CAPTION_TEMPLATES.get(theme, CAPTION_TEMPLATES["morning"]))


def send_photo(path: str, caption: Optional[str] = None):
    """Send photo via Telegram using urllib."""
    import urllib.request
    token = get_telegram_bot_token()
    filename = os.path.basename(path)
    mime_type = "image/png" if path.lower().endswith(".png") else "image/jpeg"

    with open(path, "rb") as f:
        img_data = f.read()

    boundary = "boundary_zhuzhu_photo_" + str(int(time.time()))
    caption_text = caption or ""

    def field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")

    body = (
        field("chat_id", TELEGRAM_CHAT_ID)
        + field("caption", caption_text)
        + (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8")
        + img_data
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    resp = urllib.request.urlopen(req, timeout=120)
    result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto failed: {result}")
    return result
