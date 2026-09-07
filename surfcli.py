#!/usr/bin/env python3
"""
surfcli - 해수욕장 서핑 파도 정보 조회 CLI

데이터 출처 (모두 API 키 불필요)
  - Open-Meteo Marine API   : 파고/주기/방향/스웰/풍랑/수온/조위
  - Open-Meteo Forecast API : 풍속/풍향/돌풍/기온/강수
  - Open-Meteo Geocoding    : 미등록 지점 이름 검색

사용 예시
  python surfcli.py 죽도
  python surfcli.py 중문 --days 3
  python surfcli.py 송정 --json | jq '.hours[0]'
  python surfcli.py --list
  python surfcli.py --lat 38.02 --lon 128.72 --facing 90
"""

import argparse
import json
import math
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
TZ = "Asia/Seoul"

# facing = 해변이 바다를 바라보는 방위(도). 오프쇼어 판정에 사용
SPOTS = {
    "죽도":     ("죽도해변 (양양)",   38.0233, 128.7167,  90),
    "인구":     ("인구해변 (양양)",   38.0180, 128.7185, 100),
    "기사문":   ("기사문해변 (양양)", 38.0400, 128.7050,  90),
    "하조대":   ("하조대해변 (양양)", 38.0630, 128.6950,  90),
    "동산항":   ("동산항 (양양)",     38.0100, 128.7250,  95),
    "남애":     ("남애해변 (양양)",   37.9560, 128.7690,  90),
    "낙산":     ("낙산해변 (양양)",   38.1210, 128.6350,  90),
    "금진":     ("금진해변 (강릉)",   37.6180, 129.0510,  90),
    "사천":     ("사천해변 (강릉)",   37.8480, 128.8650,  90),
    "남항진":   ("남항진해변 (강릉)", 37.7660, 128.9520,  90),
    "경포":     ("경포해변 (강릉)",   37.8020, 128.9100,  90),
    "만리포":   ("만리포 (태안)",     36.7900, 126.1450, 270),
    "용한리":   ("용한리 (포항)",     36.1500, 129.3900,  90),
    "신항만":   ("신항만 (포항)",     36.0600, 129.4200,  90),
    "송정":     ("송정해수욕장 (부산)", 35.1790, 129.1990, 135),
    "다대포":   ("다대포 (부산)",     35.0450, 128.9660, 225),
    "광안리":   ("광안리 (부산)",     35.1530, 129.1180, 180),
    "중문":     ("중문색달 (제주)",   33.2450, 126.4100, 180),
    "이호테우": ("이호테우 (제주)",   33.4980, 126.4530,   0),
    "사계":     ("사계해변 (제주)",   33.2280, 126.3120, 180),
    "월정리":   ("월정리 (제주)",     33.5560, 126.7950,   0),
    "협재":     ("협재해변 (제주)",   33.3940, 126.2400, 315),
}

DIR16 = ["북", "북북동", "북동", "동북동", "동", "동남동", "남동", "남남동",
         "남", "남남서", "남서", "서남서", "서", "서북서", "북서", "북북서"]

MARINE_VARS_FULL = [
    "wave_height", "wave_direction", "wave_period",
    "wind_wave_height", "wind_wave_period",
    "swell_wave_height", "swell_wave_direction", "swell_wave_period",
    "sea_surface_temperature", "sea_level_height_msl",
]
MARINE_VARS_CORE = ["wave_height", "wave_direction", "wave_period"]

FORECAST_VARS = [
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
    "temperature_2m", "precipitation",
]


# ---------------------------------------------------------------- helpers
def get_json(url, params, timeout=20):
    qs = urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(f"{url}?{qs}", headers={"User-Agent": "surfcli/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def compass(deg):
    if deg is None:
        return "-"
    return DIR16[int((deg % 360) / 22.5 + 0.5) % 16]


def ang_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def wind_relation(wind_from, facing):
    """바람이 불어오는 방위와 해변 방위로 오프/온쇼어 판정."""
    if wind_from is None:
        return "-", 0.0
    offshore = (facing + 180) % 360
    d = ang_diff(wind_from, offshore)
    if d <= 45:
        return "오프쇼어", 1.0
    if d <= 100:
        return "사이드쇼어", 0.5
    return "온쇼어", 0.0


def surf_score(h, period, wind_ms, wind_quality):
    """0~10 스코어. 파고 45% / 주기 30% / 바람 25%."""
    if h is None:
        return None
    # 파고: 0.8~1.8m 최적
    if h < 0.3:
        s_h = 0.0
    elif h < 0.8:
        s_h = (h - 0.3) / 0.5 * 0.7
    elif h <= 1.8:
        s_h = 1.0
    elif h <= 3.0:
        s_h = max(0.3, 1.0 - (h - 1.8) / 1.2 * 0.6)
    else:
        s_h = 0.25
    # 주기: 5초 미만 잡파, 9초 이상 좋음
    p = period or 0
    s_p = 0.0 if p < 4 else min(1.0, (p - 4) / 6.0)
    # 바람: 약할수록 + 오프쇼어일수록
    w = wind_ms if wind_ms is not None else 5
    s_w = max(0.0, 1.0 - w / 12.0) * (0.45 + 0.55 * wind_quality)
    return round((s_h * 0.45 + s_p * 0.30 + s_w * 0.25) * 10, 1)


def verdict(score):
    if score is None:
        return "데이터 없음"
    if score >= 7.5:
        return "★★★ 아주 좋음"
    if score >= 6.0:
        return "★★☆ 좋음"
    if score >= 4.5:
        return "★☆☆ 탈만함"
    if score >= 3.0:
        return "· 아쉬움"
    return "· 플랫/불가"


def bar(v, vmax=2.5, width=10):
    if v is None:
        return " " * width
    n = int(max(0.0, min(1.0, v / vmax)) * width + 0.5)
    return "█" * n + "·" * (width - n)


# ---------------------------------------------------------------- spot 조회
def resolve_spot(query, lat=None, lon=None, facing=90):
    if lat is not None and lon is not None:
        return (f"사용자 지정 ({lat:.4f}, {lon:.4f})", lat, lon, facing)
    if not query:
        raise SystemExit("지점명을 입력하거나 --lat/--lon 을 지정하세요. (--list 로 목록 확인)")

    hits = [k for k in SPOTS if query in k or query in SPOTS[k][0]]
    if len(hits) == 1:
        return SPOTS[hits[0]]
    if len(hits) > 1:
        print("여러 지점이 검색되었습니다:", file=sys.stderr)
        for k in hits:
            print(f"  - {k} ({SPOTS[k][0]})", file=sys.stderr)
        raise SystemExit(1)

    # 내장 목록에 없으면 지오코딩으로 fallback
    g = get_json(GEOCODE_URL, {"name": query, "count": 1, "language": "ko"})
    if not g.get("results"):
        raise SystemExit(f"'{query}' 지점을 찾을 수 없습니다. --list 로 목록을 확인하세요.")
    r = g["results"][0]
    print(f"[내장 목록에 없어 지오코딩 결과를 사용합니다: {r['name']}, "
          f"해변 방위는 --facing 으로 지정하면 정확해집니다]", file=sys.stderr)
    return (r["name"], r["latitude"], r["longitude"], facing)


def fetch(lat, lon, days):
    common = {"latitude": lat, "longitude": lon, "timezone": TZ, "forecast_days": days}
    try:
        marine = get_json(MARINE_URL, dict(common, hourly=",".join(MARINE_VARS_FULL)))
    except urllib.error.HTTPError:
        marine = get_json(MARINE_URL, dict(common, hourly=",".join(MARINE_VARS_CORE)))
    weather = get_json(FORECAST_URL, dict(common, hourly=",".join(FORECAST_VARS),
                                          wind_speed_unit="ms"))
    return marine, weather


def merge(marine, weather, facing):
    mh, wh = marine.get("hourly", {}), weather.get("hourly", {})
    times = mh.get("time", [])
    widx = {t: i for i, t in enumerate(wh.get("time", []))}

    def m(k, i):
        v = mh.get(k)
        return v[i] if v and i < len(v) else None

    def w(k, t):
        i = widx.get(t)
        v = wh.get(k)
        return v[i] if v is not None and i is not None and i < len(v) else None

    rows = []
    for i, t in enumerate(times):
        wd = w("wind_direction_10m", t)
        rel, q = wind_relation(wd, facing)
        ws = w("wind_speed_10m", t)
        h = m("wave_height", i)
        p = m("wave_period", i)
        rows.append({
            "time": t,
            "wave_height": h,
            "wave_period": p,
            "wave_direction": m("wave_direction", i),
            "swell_height": m("swell_wave_height", i),
            "swell_period": m("swell_wave_period", i),
            "swell_direction": m("swell_wave_direction", i),
            "wind_wave_height": m("wind_wave_height", i),
            "sea_temp": m("sea_surface_temperature", i),
            "tide": m("sea_level_height_msl", i),
            "wind_speed": ws,
            "wind_gust": w("wind_gusts_10m", t),
            "wind_direction": wd,
            "wind_relation": rel,
            "air_temp": w("temperature_2m", t),
            "precip": w("precipitation", t),
            "score": surf_score(h, p, ws, q),
        })
    return rows


# ---------------------------------------------------------------- 출력
def fmt(v, unit="", nd=1):
    return f"{v:.{nd}f}{unit}" if isinstance(v, (int, float)) else "-"


def render(name, lat, lon, facing, rows, start_h, end_h, show_all):
    now = datetime.now()
    print()
    print(f"  {name}   ({lat:.4f}, {lon:.4f})  해변방위 {compass(facing)}")
    print(f"  조회 {now:%Y-%m-%d %H:%M} KST   출처 Open-Meteo (ICON-Wave / GFS-Wave)")
    print("─" * 92)

    cur = min(rows, key=lambda r: abs(datetime.fromisoformat(r["time"]) - now), default=None)
    if cur:
        print(f"  현재  파고 {fmt(cur['wave_height'],'m',2)}  "
              f"주기 {fmt(cur['wave_period'],'s')}  "
              f"파향 {compass(cur['wave_direction'])}  |  "
              f"바람 {fmt(cur['wind_speed'],'m/s')} {compass(cur['wind_direction'])} "
              f"({cur['wind_relation']})  돌풍 {fmt(cur['wind_gust'],'m/s')}")
        print(f"        스웰 {fmt(cur['swell_height'],'m',2)} / {fmt(cur['swell_period'],'s')} "
              f"{compass(cur['swell_direction'])}   풍랑 {fmt(cur['wind_wave_height'],'m',2)}  |  "
              f"수온 {fmt(cur['sea_temp'],'℃')}  기온 {fmt(cur['air_temp'],'℃')}  "
              f"조위 {fmt(cur['tide'],'m',2)}")
        print(f"        서핑 컨디션  {fmt(cur['score'],'',1)}/10   {verdict(cur['score'])}")
    print("─" * 92)
    print("  시각      파고    주기   파향   스웰(m/s)      바람          구분     조위    점수")
    print("─" * 92)

    day = None
    for r in rows:
        dt = datetime.fromisoformat(r["time"])
        if not show_all and not (start_h <= dt.hour <= end_h):
            continue
        if dt.strftime("%m/%d") != day:
            day = dt.strftime("%m/%d")
            print(f"  [{day} {'월화수목금토일'[dt.weekday()]}]")
        swell = f"{fmt(r['swell_height'],'',1)}/{fmt(r['swell_period'],'',0)}"
        print(f"   {dt:%H:%M}  {bar(r['wave_height'])} {fmt(r['wave_height'],'m',2):>6} "
              f"{fmt(r['wave_period'],'s',0):>4} {compass(r['wave_direction']):>4} "
              f"{swell:>8}  {fmt(r['wind_speed'],'m/s'):>7} {compass(r['wind_direction']):>4} "
              f"{r['wind_relation']:>5}  {fmt(r['tide'],'m',2):>6}  "
              f"{fmt(r['score'],'',1):>4} {verdict(r['score'])}")
    print("─" * 92)
    best = max((r for r in rows if r["score"] is not None), key=lambda r: r["score"], default=None)
    if best:
        bt = datetime.fromisoformat(best["time"])
        print(f"  베스트 타임  {bt:%m/%d %H:%M}  "
              f"파고 {fmt(best['wave_height'],'m',2)} / 주기 {fmt(best['wave_period'],'s')} / "
              f"{best['wind_relation']} {fmt(best['wind_speed'],'m/s')}  → {best['score']}/10")
    print("  ※ 전지구 파랑모델 기반 추정치입니다. 입수 전 기상청 해상예보와 현장 상황을 확인하세요.")
    print()


def main():
    ap = argparse.ArgumentParser(description="해수욕장 서핑 파도 정보 조회")
    ap.add_argument("spot", nargs="?", help="해수욕장 이름 (부분 일치)")
    ap.add_argument("--list", action="store_true", help="등록된 지점 목록")
    ap.add_argument("--days", type=int, default=2, help="예보 일수 (기본 2, 최대 7)")
    ap.add_argument("--lat", type=float), ap.add_argument("--lon", type=float)
    ap.add_argument("--facing", type=int, default=90, help="해변이 바라보는 방위(도)")
    ap.add_argument("--from", dest="start_h", type=int, default=5, help="표시 시작 시각")
    ap.add_argument("--to", dest="end_h", type=int, default=20, help="표시 종료 시각")
    ap.add_argument("--all-hours", action="store_true", help="24시간 전부 표시")
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    a = ap.parse_args()

    if a.list:
        print("\n  등록된 지점")
        for k, (n, la, lo, f) in SPOTS.items():
            print(f"   {k:<9} {n:<20} {la:8.4f},{lo:9.4f}  방위 {compass(f)}")
        print("\n  목록에 없는 곳은 이름으로 검색하거나 --lat/--lon 을 쓰세요.\n")
        return

    name, lat, lon, facing = resolve_spot(a.spot, a.lat, a.lon, a.facing)
    try:
        marine, weather = fetch(lat, lon, max(1, min(a.days, 7)))
    except urllib.error.URLError as e:
        raise SystemExit(f"API 호출 실패: {e}")
    rows = merge(marine, weather, facing)
    if not rows:
        raise SystemExit("해양 데이터가 없는 지점입니다. 내륙이거나 모델 격자 밖일 수 있습니다.")

    if a.json:
        print(json.dumps({"spot": name, "lat": lat, "lon": lon,
                          "facing": facing, "hours": rows},
                         ensure_ascii=False, indent=2))
    else:
        render(name, lat, lon, facing, rows, a.start_h, a.end_h, a.all_hours)


if __name__ == "__main__":
    main()
