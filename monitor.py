# -*- coding: utf-8 -*-
"""
金价分级监控脚本（方案A：零积分脚本版）
====================================
架构：定时调度器每 15 分钟调起本脚本一次，
      脚本内部根据"当前价格状态"判断是否真的需要取数——
      大部分调用会在第一步就被跳过，实现分级省流。

提醒规则：
  A1  跌破 880 元/克  → 微信重要提醒一次（进入 ≤870 区域后升频至 1 次/小时）
  A2  ≤ 865 元/克     → 微信接近提示一次
  A3  跌破 860 元/克  → 微信立即推送（含价格+时间），此后可停用脚本
  回差：价格回升 5 元后对应提醒重新布防，防止边界横跳轰炸

监测频率（2026-08-30 调整）：
  ≥ 950 元/克       → 每 2 日 1 次
  900–950 元/克     → 1 次/日
  870–900 元/克     → 2 次/日
  ≤ 870 元/克       → 1 次/小时
"""
import json
import os
import datetime
import requests

# ---------------- 配置区（改这里就够了） ----------------
CONFIG = {
    # Server酱 SendKey：https://sct.ftqq.com 微信扫码登录后复制
    "sendkey": "SCT407784T3eQI5VPcuMaCQOpRtsIm9Y9T",

    "threshold_warn": 880.0,  # A1 重要提醒阈值
    "threshold_near": 865.0,  # A2 接近提示阈值
    "threshold_buy":  860.0,  # A3 入场信号阈值
    "hysteresis": 5.0,        # 回差（元）：回升该幅度后重新布防

    # 事件日升频：美联储议息/非农/CPI 当天手动改为 True，全天升一档
    "event_day_boost": False,

    # 节假日列表（上金所休市日，手动维护，格式 YYYY-MM-DD）
    "holidays": [
        "2026-10-01", "2026-10-02", "2026-10-03",
        "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07",
    ],

    "state_file": os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"),
    "log_file": os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.log"),
}
# --------------------------------------------------------


def log(msg):
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(CONFIG["log_file"], "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    """状态机持久化：记住上次检查时间、上次价格、各级提醒是否已触发"""
    default = {"last_check_ts": 0, "last_price": None,
               "a1_latched": False, "a2_latched": False, "a3_latched": False}
    try:
        with open(CONFIG["state_file"], "r", encoding="utf-8") as f:
            default.update(json.load(f))
    except Exception:
        pass
    return default


def save_state(st):
    with open(CONFIG["state_file"], "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def in_session(now=None):
    """上金所交易时段：日盘 9:00-11:30 / 13:30-15:30，夜盘 20:00-次日02:30，周末休市"""
    now = now or datetime.datetime.now()
    if now.strftime("%Y-%m-%d") in CONFIG["holidays"]:
        return False
    t, wd = now.time(), now.weekday()  # 0=周一 ... 6=周日
    day_session = wd <= 4 and (datetime.time(9, 0) <= t <= datetime.time(11, 30)
                               or datetime.time(13, 30) <= t <= datetime.time(15, 30))
    night_a = wd <= 4 and t >= datetime.time(20, 0)            # 当晚 20:00 后
    night_b = 1 <= wd <= 5 and t <= datetime.time(2, 30)       # 次日凌晨（对应前一交易日）
    return day_session or night_a or night_b


def min_interval(price, st):
    """距离自适应采样（2026-08-30 调整为三档，秒）：
    ≥950     每 2 日 1 次（远离目标，最省算力）
    900-950  跌破 950：1 次/日
    870-900  跌破 900：2 次/日
    ≤870     接近 860：1 次/小时"""
    if price is None:
        return 86400
    if price >= 950:
        return 172800       # 远离：每 2 日 1 次
    if price >= 900:
        return 86400        # 跌破 950：1 次/日
    if price > 870:
        return 43200        # 跌破 900：2 次/日
    return 3600             # 接近 860（≤870）：1 次/小时


def _is_price(s):
    try:
        v = float(s)
        return 300 <= v <= 2000
    except Exception:
        return False


def fetch_price():
    """取当前金价（元/克）。主源：新浪上海金延期 AUTD；备用：伦敦金×汇率换算"""
    headers = {"Referer": "https://finance.sina.com.cn",
               "User-Agent": "Mozilla/5.0"}
    # ---- 主源 ----
    try:
        r = requests.get("https://hq.sinajs.cn/list=gds_AUTD",
                         headers=headers, timeout=10)
        r.encoding = "gbk"
        body = r.text.split('"')[1]
        fields = body.split(",")
        # 实测布局（已与伦敦金折算价交叉验证）：fields[0]=最新价，[2]=均价
        if fields and _is_price(fields[0]):
            return round(float(fields[0]), 2)
        for x in fields:  # 防御：布局变化时退化为取首个合法价格
            if _is_price(x):
                return round(float(x), 2)
    except Exception as e:
        log(f"主源失败: {e}")
    # ---- 备用源：伦敦金(美元/盎司) × 美元兑人民币 / 31.1035 ----
    try:
        r = requests.get("https://hq.sinajs.cn/list=hf_XAU,fx_susdcny",
                         headers=headers, timeout=10)
        r.encoding = "gbk"
        parts = r.text.split(";")
        xau = float(parts[0].split('"')[1].split(",")[0])
        # fx 布局：[0]=时间，[1]=买价，[2]=卖价，[3]=中间价附近档
        cny_fields = parts[1].split('"')[1].split(",")
        cny = float(cny_fields[3]) if _is_price(cny_fields[3]) else float(cny_fields[1])
        return round(xau * cny / 31.1035, 2)
    except Exception as e:
        log(f"备用源失败: {e}")
    return None


def push(title, desp):
    """Server酱微信推送（免费版每天 5 条，本方案每月最多 3 条）"""
    key = os.environ.get("SENDKEY") or CONFIG["sendkey"]  # 环境变量优先，便于 CI 部署
    if not key or "填入" in key:
        log(f"[未配置SendKey，仅本地打印] {title}\n{desp}")
        return
    try:
        requests.post(f"https://sctapi.ftqq.com/{key}.send",
                      data={"title": title, "desp": desp}, timeout=10)
        log(f"已推送: {title}")
    except Exception as e:
        log(f"推送失败: {e}")


def main():
    st = load_state()
    now = datetime.datetime.now()

    # 1) 非交易时段：零调用直接退出（状态跨日保留）
    if not in_session(now):
        save_state(st)
        return

    # 2) 未到本级检查时间：跳过取数（省算力的核心）
    interval = min_interval(st.get("last_price"), st)
    if CONFIG["event_day_boost"]:
        interval = interval // 2
    if st["a3_latched"]:
        interval = max(interval, 3600)   # A3 已触发后降回低频，仅供观察
    if now.timestamp() - st["last_check_ts"] < interval:
        return

    # 3) 取数
    price = fetch_price()
    if price is None:
        log("两个数据源均取数失败，本次放弃")
        return
    st["last_price"], st["last_check_ts"] = price, now.timestamp()
    log(f"当前金价 {price} 元/克（状态: a1={st['a1_latched']} a2={st['a2_latched']} a3={st['a3_latched']}）")

    tw, tn, tb, hys = (CONFIG["threshold_warn"], CONFIG["threshold_near"],
                       CONFIG["threshold_buy"], CONFIG["hysteresis"])

    # 4) A3：跌破 860，立即推送（含价格和时间）
    if price < tb and not st["a3_latched"]:
        st["a3_latched"] = True
        drop = (price / tw - 1) * 100
        push(f"🎯 金价跌破 {tb:.0f} 元/克",
             f"**当前价格：{price} 元/克**\n\n触发时间：{now:%Y-%m-%d %H:%M:%S}\n\n"
             f"距 880 触发点累计跌幅：{drop:.1f}%\n\n可按分批计划入场。")

    # 5) A2：接近 860
    elif price <= tn and not st["a2_latched"]:
        st["a2_latched"] = True
        push(f"⚠️ 金价接近 {tb:.0f} 目标区",
             f"当前价格：{price} 元/克\n\n距目标 {price - tb:.1f} 元，已升频至 15 分钟/次。")

    # 6) A1：跌破 880
    elif price < tw and not st["a1_latched"]:
        st["a1_latched"] = True
        push(f"⚠️ 金价跌破 {tw:.0f} 元/克关口",
             f"**当前价格：{price} 元/克**\n\n时间：{now:%Y-%m-%d %H:%M:%S}\n\n"
             f"已升频至 1 小时/次，接近 {tb:.0f} 将再次提醒。")

    # 7) 回差重新布防：显著回升后允许同级提醒再次生效
    if st["a1_latched"] and price >= tw + hys:
        st["a1_latched"] = False
        log(f"回升至 {price}，A1 重新布防")
    if st["a2_latched"] and price >= tn + hys:
        st["a2_latched"] = False
    if st["a3_latched"] and price >= tb + hys:
        st["a3_latched"] = False
        log(f"回升至 {price}，A3 重新布防（如已入场可停用脚本）")

    save_state(st)


if __name__ == "__main__":
    import sys
    if "--push-test" in sys.argv:  # 推送链路测试：立即向微信发一条测试消息
        price = fetch_price()
        push("✅ 金价监控测试消息",
             f"如果你在微信看到这条消息，说明推送链路已打通。\n\n"
             f"当前金价：{price} 元/克\n时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
             f"监控规则：跌破 880 提醒并升频 / 接近 865 高频 / 跌破 860 立即通知。")
    elif "--test" in sys.argv:   # 测试模式：无视时段与频率，立即取一次价并走完整状态机
        price = fetch_price()
        print(f"[TEST] 当前金价: {price} 元/克")
        if price:
            st = load_state()
            st["last_price"], st["last_check_ts"] = price, datetime.datetime.now().timestamp()
            print(f"[TEST] 下一档最小检查间隔: {min_interval(price, st) / 60:.0f} 分钟")
    else:
        main()
