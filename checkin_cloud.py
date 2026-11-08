# -*- coding: utf-8 -*-
"""
WorkBuddy 每日签到 —— 云端版（GitHub Actions 用）

特点：
  1. 幂等：先查状态，今天已签到（无论是本地签的还是云端签的）就不重复领。
  2. 自续期：accessToken 过期自动用 refreshToken 换新；新令牌加密后写回 state.enc.json，
     由 workflow 提交回仓库，所以可以长期无人值守。
  3. 顺带产出 dashboard.html / index.html（GitHub Pages 直接访问）。

环境变量：
  WB_STATE_KEY  AES-256-GCM 用的口令（仓库 Secret），唯一需要保密的东西
  WB_UID        账号 ID（明文，非敏感）
"""
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.enc.json")
DATA = os.path.join(HERE, "data.json")
TEMPLATE = os.path.join(HERE, "template.html")
DASH = os.path.join(HERE, "dashboard.html")
INDEX = os.path.join(HERE, "index.html")

HOST = "https://www.workbuddy.cn"
API_STATUS = "/v2/billing/meter/checkin-activity-status"
API_STATUS_ALT = "/v2/billing/meter/checkin-status"
API_CHECKIN = "/v2/billing/meter/daily-checkin"
API_REFRESH = "/v2/plugin/auth/token/refresh"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

CN = timezone(timedelta(hours=8))


def cst_now():
    return datetime.now(CN)


def log(msg=""):
    print(msg, flush=True)


# ------------------------- 状态文件加解密 -------------------------
def state_key():
    raw = os.environ.get("WB_STATE_KEY", "").strip()
    if not raw:
        raise SystemExit("缺少环境变量 WB_STATE_KEY")
    return hashlib.sha256(raw.encode("utf-8")).digest()


def load_state():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not os.path.exists(STATE):
        return {}
    blob = json.load(open(STATE, encoding="utf-8"))
    pt = AESGCM(state_key()).decrypt(base64.b64decode(blob["nonce"]),
                                     base64.b64decode(blob["ct"]),
                                     b"wb-checkin-state-v1")
    return json.loads(pt.decode("utf-8"))


def save_state(st):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(state_key()).encrypt(nonce, json.dumps(st, ensure_ascii=False).encode("utf-8"),
                                     b"wb-checkin-state-v1")
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "alg": "AES-256-GCM",
                   "nonce": base64.b64encode(nonce).decode(),
                   "ct": base64.b64encode(ct).decode()}, f, indent=1)


# ------------------------- HTTP -------------------------
def api(path, token, uid, body=None):
    payload = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(HOST + path, data=payload, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept-Language": "zh-CN",
                                          "Authorization": "Bearer " + token,
                                          "X-User-Id": uid,
                                          "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"raw": txt[:300]}
    except Exception as e:
        return -1, {"error": repr(e)}


def refresh_tokens(st):
    rt = st.get("refreshToken")
    if not rt:
        return st, False
    req = urllib.request.Request(HOST + API_REFRESH, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Refresh-Token": rt,
                                          "X-Auth-Refresh-Source": "plugin",
                                          "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log("  × 刷新令牌失败：" + repr(e)[:160])
        return st, False
    d = body.get("data") or {}
    if isinstance(d.get("data"), dict):
        d = d["data"]
    at = d.get("accessToken")
    if not at:
        log("  × 刷新返回里没有 accessToken：" + str(body)[:160])
        return st, False
    st["accessToken"] = at
    st["refreshToken"] = d.get("refreshToken") or rt
    exp = d.get("expiresIn")
    if isinstance(exp, (int, float)):
        st["accessExpiresAt"] = int((cst_now() + timedelta(seconds=float(exp))).timestamp() * 1000)
    rexp = d.get("refreshExpiresIn")
    if isinstance(rexp, (int, float)):
        st["refreshExpiresAt"] = int((cst_now() + timedelta(seconds=float(rexp))).timestamp() * 1000)
    log("  ✓ 令牌已刷新")
    return st, True


# ------------------------- 业务 -------------------------
def get_status(token, uid):
    code, body = api(API_STATUS, token, uid)
    if code == 404:
        code, body = api(API_STATUS_ALT, token, uid)
    return code, body


def do_checkin(st, uid):
    """返回 (ok, info, changed)"""
    token = st.get("accessToken")
    code, body = get_status(token, uid)
    info = {"stage": "status", "http": code, "body": body}

    if code in (401, 403):
        st, changed = refresh_tokens(st)
        if not changed:
            return False, info, False
        token = st["accessToken"]
        code, body = get_status(token, uid)
        info = {"stage": "status", "http": code, "body": body}

    if code != 200 or body.get("code") not in (0, "0"):
        return False, info, False

    data = body.get("data") or {}
    info["data"] = data
    claimed_now = False
    if not data.get("today_checked_in"):
        c2, b2 = api(API_CHECKIN, token, uid)
        info["claim_http"] = c2
        info["claim"] = b2
        ccode = b2.get("code")
        msg = str(b2.get("msg") or "")
        if c2 == 200 and ccode in (0, "0"):
            claimed_now = True
            log("  ✓ 云端领取成功")
            # 领完再查一次，拿到最新连续天数
            c3, b3 = get_status(token, uid)
            if c3 == 200 and isinstance(b3.get("data"), dict):
                data = b3["data"]
                info["data"] = data
        elif str(ccode) == "10001" or "已签" in msg:
            log("  • 今天已经签过了（本地已完成），云端只记录不同步领取")
            data["today_checked_in"] = True
            info["data"] = data
        else:
            log("  × 领取失败：HTTP %s %s" % (c2, msg[:120]))
            return False, info, False
    else:
        log("  • 今天已经签过了（本地已完成），云端不重复领取")

    info["claimed_now"] = claimed_now
    return True, info, False


def build_payload(data, st, ok, claimed_now, extra):
    now = cst_now()
    today = now.date()
    dates = set(data.get("checkin_dates") or []) if isinstance(data.get("checkin_dates"), list) else set()

    days = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        days.append({"d": d.day, "m": d.month, "ok": d.isoformat() in dates,
                     "today": i == 0})

    rexp = st.get("refreshExpiresAt")
    if isinstance(rexp, (int, float)):
        dt = datetime.fromtimestamp(rexp / 1000, CN)
        token_expire = dt.strftime("%Y-%m-%d")
        days_left = (dt.date() - today).days
    else:
        token_expire, days_left = "自动续期", None

    return {
        "mode": "cloud",
        "stamp": "云端 · 每天 23:58",
        "points": data.get("total_credits"),
        "capacity": None,
        "packs": None,
        "plan": data.get("theme_name") or data.get("activity_name") or "Buddy加油站",
        "todayClaimed": bool(data.get("today_checked_in")),
        "todayCredit": data.get("today_credit") or data.get("daily_credit") or 0,
        "streak": data.get("streak_days"),
        "nextReward": (str(data.get("streak_bonus_credit")) + " 分") if data.get("streak_bonus_credit") else "—",
        "lastRun": now.strftime("%Y-%m-%d %H:%M"),
        "nextRun": "明天 23:58（云端）",
        "tokenExpire": token_expire,
        "tokenDaysLeft": days_left,
        "days": days,
        "details": [{"name": data.get("theme_name") or "Buddy加油站",
                     "amount": data.get("total_credits") or 0}],
        "ok": ok,
        "claimedByCloud": claimed_now,
        "note": extra,
    }


def render(payload):
    html = open(TEMPLATE, encoding="utf-8").read()
    html = html.replace("/*__DATA__*/{}", json.dumps(payload, ensure_ascii=False))
    for p in (DASH, INDEX):
        with open(p, "w", encoding="utf-8") as f:
            f.write(html)
    return DASH


def main():
    uid = os.environ.get("WB_UID", "").strip()
    log("=" * 46)
    log("WorkBuddy 签到 · %s" % cst_now().strftime("%Y-%m-%d %H:%M:%S"))
    log("=" * 46)

    st = load_state()
    if not st.get("accessToken") and not st.get("refreshToken"):
        log("× state.enc.json 里没有令牌，无法继续")
        return 2

    ok, info, changed = do_checkin(st, uid)
    data = info.get("data") or {}

    # 记录运行历史
    hist = []
    try:
        hist = json.load(open(DATA, encoding="utf-8")).get("runs", [])
    except Exception:
        pass
    hist.append({"at": cst_now().strftime("%Y-%m-%d %H:%M"),
                 "ok": bool(ok),
                 "claimed": bool(info.get("claimed_now")),
                 "checked_in": bool(data.get("today_checked_in"))})
    hist = hist[-90:]
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump({"runs": hist}, f, indent=1)

    if not ok:
        log("× 本次签到没有完成：" + json.dumps(info, ensure_ascii=False)[:400])

    extra = "云端已签" if info.get("claimed_now") else ("本地已签，云端仅记录" if ok else "签到失败")
    payload = build_payload(data, st, ok, bool(info.get("claimed_now")), extra)
    render(payload)
    log("看板已生成：dashboard.html / index.html")

    save_state(st)
    log("令牌状态已加密写回 state.enc.json")

    if ok:
        log("完成：积分 %s，连续 %s 天，今日 %s 分" %
            (payload["points"], payload["streak"], payload["todayCredit"]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
