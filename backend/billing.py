# -*- coding: utf-8 -*-
"""积分 / 套餐 / 单张成本。

汇率锚点：1 元 = 10 积分（CNY_PER_CREDIT=0.1）。
单次生成：GEN_CREDITS 积分（默认 8 = 0.8 元/张；老板要 5 积分就在 .env 写 GEN_CREDITS=5）。

单张成本（APIMart Gold 价，2026-10 页面价，汇率 USD_CNY）：
    gpt-image-2 / -ext @1K      $0.0085  → 约 0.061 元/张   ← 当前默认
    gemini-2.5-flash-image      $0.0125  → 约 0.089 元/张   （nano banana）
    seedream-5-0-flash          $0.0137  → 约 0.098 元/张
    dall-e-3                    $0.032   → 约 0.229 元/张
    seedream-4-5                $0.26    → 约 1.86 元/张    ← 卖 0.8 元必亏，锁进高级档
    seedream-5-0-pro            $0.36    → 约 2.57 元/张    ← 同上
    flux-2-pro 1MP              $0.24    → 约 1.72 元/张
所以模型不是"随便选"的：便宜档随便卖，贵档必须按倍率加积分，否则一张亏一张。
"""
import os

USD_CNY = float(os.environ.get("USD_CNY", "7.15"))
CNY_PER_CREDIT = 0.1          # 1 元 = 10 积分


def _i(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


GEN_CREDITS = _i("GEN_CREDITS", 8)        # 标准档 1 张
FREE_GENS = _i("FREE_GENS", 3)            # 新用户免费次数
USER_MAX_N = _i("USER_MAX_N", 2)          # 普通用户一次最多出几张
ADMIN_MAX_N = _i("ADMIN_MAX_N", 4)

# 每模型单张美元价。不在表里的按 STD 估（$0.012）。
MODEL_USD = {
    "gpt-image-2": 0.0085, "gpt-image-2-ext": 0.0085, "gpt-image-2-official": 0.0106,
    "gemini-2.5-flash-image-preview": 0.0125, "gemini-2.5-flash-image": 0.0125,
    "gemini-2.5-flash-image-preview-official": 0.0156,
    "gemini-3-pro-image-preview": 0.03, "gemini-3.1-flash-image-preview": 0.02,
    "nano-banana-ext": 0.0125, "nano-banana-2-ext": 0.015, "nano-banana-2-lite-ext": 0.0125,
    "nano-banana-pro-ext": 0.03, "gemini-nano-banana-2.1-ext": 0.02,
    "seedream-5-0-flash": 0.0137, "seedream-4-0": 0.195, "seedream-4-5": 0.26,
    "seedream-5-0-lite": 0.28, "seedream-5-0-pro": 0.36,
    "dall-e-3": 0.032, "imagen-4.0-apimart": 0.04,
    "z-image-turbo": 0.01, "qwen-image-3.0": 0.0206, "qwen-image-3.0-pro": 0.0286,
    "flux-3-image-klein": 0.072, "flux-3-image": 0.384, "flux-2-pro": 0.24,
    "flux-2-flex": 0.4, "flux-2-max": 0.8, "flux-kontext-pro": 0.32, "flux-kontext-max": 0.64,
    "gpt-image-1": 0.04,
}
STD_USD = 0.012

# 档位：按美元价决定积分倍率 + 是否允许普通用户用
TIERS = ((0.02, 1, "标准"), (0.10, 2, "高级"), (999.0, 5, "旗舰"))
BLOCK_MAX_FOR_USER = True     # 旗舰档（>0.10 美元）不给普通用户选


def usd_of(model):
    return MODEL_USD.get((model or "").strip(), STD_USD)


def tier_of(model):
    u = usd_of(model)
    for lim, mul, name in TIERS:
        if u <= lim:
            return {"usd": u, "mul": mul, "name": name, "cny": round(u * USD_CNY, 4)}
    return {"usd": u, "mul": 5, "name": "旗舰", "cny": round(u * USD_CNY, 4)}


def cny_of(model):
    return round(usd_of(model) * USD_CNY, 4)


def credits_for(model=None, n=1):
    """这一单要扣多少积分（含张数）。"""
    t = tier_of(model)
    return int(GEN_CREDITS * t["mul"] * max(1, int(n or 1)))


def cny_cost(model=None, n=1):
    """这一单的模型成本（元）。"""
    return round(cny_of(model) * max(1, int(n or 1)), 4)


def cny_income(credits):
    """积分折合收入（元）。"""
    return round(credits * CNY_PER_CREDIT, 4)


# ---------------- 套餐：固定积分，赠分体现在 "≈多少次" 上 ----------------
PACKAGES = [
    {"id": "trial",  "name": "体验包",  "price": 9.9,  "credits": 100,  "tag": "",
     "desc": "先试试，够做十几张"},
    {"id": "basic",  "name": "常用包",  "price": 29.9, "credits": 360,  "tag": "最划算",
     "desc": "日常自用、给顾客看效果"},
    {"id": "pro",    "name": "专业包",  "price": 99.0, "credits": 1400, "tag": "店家推荐",
     "desc": "发型师 / 门店高频使用"},
    {"id": "max",    "name": "旗舰包",  "price": 299.0, "credits": 5000, "tag": "单价最低",
     "desc": "团队共用、长期批量"},
]


def packages():
    out = []
    for p in PACKAGES:
        q = dict(p)
        q["gens"] = int(p["credits"] / max(1, GEN_CREDITS))
        q["per"] = round(p["price"] / max(1, q["gens"]), 2)
        q["per_credit"] = round(p["price"] / p["credits"], 4)
        out.append(q)
    return out


def package(pid):
    for p in PACKAGES:
        if p["id"] == pid:
            return p
    return None


def profile():
    """给前台 / 后台看的一整套价格口径。"""
    cur = os.environ.get("APIMART_IMAGE_MODEL") or ""
    t = tier_of(cur)
    return {
        "cny_per_credit": CNY_PER_CREDIT,
        "gen_credits": GEN_CREDITS,
        "free_gens": FREE_GENS,
        "user_max_n": USER_MAX_N,
        "usd_cny": USD_CNY,
        "packages": packages(),
        "current_model": cur,
        "current_tier": t,
        "current_cny": cny_of(cur),
        "current_income": cny_income(credits_for(cur, 1)),
    }


def margin(model=None, n=1):
    """单张毛利估算（元 / 毛利率）。不含服务器固定成本。"""
    cr = credits_for(model, n)
    inc = cny_income(cr)
    cost = cny_cost(model, n)
    # 附加：存储/CDN 0.004 元 + 支付费率 0.6% + 失败重跑 8%
    extra = 0.004 * max(1, int(n or 1)) + inc * 0.006 + cost * 0.08
    total = round(cost + extra, 4)
    return {"credits": cr, "income": inc, "api_cost": cost, "extra": round(extra, 4),
            "total_cost": total, "gross": round(inc - total, 4),
            "rate": round((inc - total) / inc, 4) if inc else 0.0}
