#!/usr/bin/env python3
"""
buoy.py - 기상청 API허브 부이 실측 조회 / Open-Meteo 모델 대조

준비
  export KMA_API_KEY="발급받은키"

명령
  python3 buoy.py nearest --all       해변별 최근접 부이 거리표  ← 먼저 이걸 보세요
  python3 buoy.py nearest 죽도        해당 해변 인근 관측지점
  python3 buoy.py obs                 전체 실황
  python3 buoy.py obs --near 죽도     해변 근처만
  python3 buoy.py compare 죽도        실측 vs Open-Meteo 모델 (파고·수온)
  python3 buoy.py log 죽도            대조 결과를 calib.csv 에 누적
  python3 buoy.py detail 22101        해양기상부이 상세 (파주기·파향)
  python3 buoy.py raw sea_obs         응답 원문 출력

출처: 기상청 API허브 (공공누리 제1유형, 출처표시)
"""

import argparse, csv, json, math, os, sys
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta

TYP01 = "https://apihub.kma.go.kr/api/typ01/url"
MARINE = "https://marine-api.open-meteo.com/v1/marine"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "stations.json")
CALIB = os.path.join(HERE, "calib.csv")

# sea_obs.php 응답의 실제 컬럼 순서 (쉼표 구분, 문서에 없는 LON/LAT 포함)
SEA_OBS_COLS = ["TP", "TM", "STN_ID", "STN_KO", "LON", "LAT",
                "WH", "WD", "WS", "WS_GST", "TW", "TA", "PA", "HM"]
# kma_buoy.php 는 문서 기준 (첫 실행 후 raw 로 검증 필요)
BUOY_COLS = ["TM", "STN", "WD1", "WS1", "WS1_GST", "WD2", "WS2", "WS2_GST",
             "PA", "HM", "TA", "TW", "WH_MAX", "WH_SIG", "WH_AVE", "WP", "WO"]

TP_NAME = {"B": "해양기상부이", "C": "파고부이", "D": "표류부이",
           "L": "등표", "J": "기상1호"}
WAVE_TP = {"B", "C", "D", "L"}          # 파고를 관측하는 장비
MISSING = {-9.0, -99.0, -999.0, -9999.0}


# ---------------------------------------------------------------- 해변
def load_spots():
    try:
        sys.path.insert(0, HERE)
        from surfcli import SPOTS
        return {k: {"name": v[0], "lat": v[1], "lon": v[2]} for k, v in SPOTS.items()}
    except Exception:
        return {"죽도": {"name": "죽도해변 (양양)", "lat": 38.0233, "lon": 128.7167},
                "하조대": {"name": "하조대해변 (양양)", "lat": 38.0630, "lon": 128.6950},
                "금진": {"name": "금진해변 (강릉)", "lat": 37.6180, "lon": 129.0510},
                "만리포": {"name": "만리포 (태안)", "lat": 36.7900, "lon": 126.1450},
                "송정": {"name": "송정해수욕장 (부산)", "lat": 35.1790, "lon": 129.1990},
                "중문": {"name": "중문색달 (제주)", "lat": 33.2450, "lon": 126.4100}}


SPOTS = load_spots()


def find_spot(q):
    hits = [k for k in SPOTS if q in k or q in SPOTS[k]["name"]]
    if not hits:
        raise SystemExit(f"'{q}' 해변을 찾을 수 없습니다.")
    if len(hits) > 1:
        raise SystemExit("여러 해변이 검색됨: " + ", ".join(hits))
    return dict(SPOTS[hits[0]], key=hits[0])


def haversine(a1, o1, a2, o2):
    R = 6371.0
    p1, p2 = math.radians(a1), math.radians(a2)
    dp, dl = math.radians(a2 - a1), math.radians(o2 - o1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


# ---------------------------------------------------------------- HTTP
def key():
    k = os.environ.get("KMA_API_KEY")
    if not k:
        raise SystemExit('환경변수 KMA_API_KEY 가 없습니다.\n  export KMA_API_KEY="발급받은키"')
    return k


def fetch(path, params, timeout=25):
    url = f"{TYP01}/{path}?{urllib.parse.urlencode(dict(params, authKey=key()))}"
    req = urllib.request.Request(url, headers={"User-Agent": "buoy-cli/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} — 인증키를 확인하세요.")
    except urllib.error.URLError as e:
        raise SystemExit(f"연결 실패: {e.reason}")
    for enc in ("euc-kr", "cp949", "utf-8"):      # 응답은 EUC-KR
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("euc-kr", "replace")


# ---------------------------------------------------------------- 파싱
def parse_csvish(text, cols):
    """'#' 주석을 걷어내고 쉼표로 끊어 컬럼에 위치로 대응시킨다."""
    rows = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or "7777" in s:
            continue
        parts = [p.strip() for p in s.rstrip("=").rstrip(",").split(",")]
        if len(parts) < len(cols):
            continue
        rows.append(dict(zip(cols, parts[:len(cols)])))
    return rows


def f(row, k):
    """숫자로 바꾸되 결측(-9/-99/-999)은 None."""
    v = row.get(k, "").strip()
    if not v:
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return None if x in MISSING else x


def obs_time(dt=None):
    """관측은 정시·30분. 생산 지연 15분을 빼고 내림."""
    dt = (dt or datetime.now()) - timedelta(minutes=15)
    return dt.replace(minute=0 if dt.minute < 30 else 30,
                      second=0, microsecond=0).strftime("%Y%m%d%H%M")


def sea_obs(tm=None):
    return parse_csvish(fetch("sea_obs.php", {"tm": tm or obs_time(), "stn": 0, "help": 0}),
                        SEA_OBS_COLS)


# ---------------------------------------------------------------- 지점
def stations(refresh=False):
    """sea_obs 응답에 좌표가 들어 있으므로 별도 지점 API가 필요 없다."""
    if not refresh and os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as fp:
            return json.load(fp)
    out = []
    for r in sea_obs():
        lat, lon = f(r, "LAT"), f(r, "LON")
        if lat is None or lon is None:
            continue
        out.append({"tp": r["TP"], "kind": TP_NAME.get(r["TP"], r["TP"]),
                    "id": r["STN_ID"], "name": r["STN_KO"], "lat": lat, "lon": lon})
    if not out:
        raise SystemExit("지점을 얻지 못했습니다. `buoy.py raw sea_obs` 로 원문을 확인하세요.")
    with open(CACHE, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)
    return out


def nearest_for(spot, n=5, wave_only=True):
    ss = [s for s in stations() if not wave_only or s["tp"] in WAVE_TP]
    for s in ss:
        s["km"] = haversine(spot["lat"], spot["lon"], s["lat"], s["lon"])
    return sorted(ss, key=lambda s: s["km"])[:n]


def verdict(km):
    return "쓸만함" if km <= 20 else ("참고용" if km <= 50 else "의미 약함")


def cmd_nearest(a):
    stations(refresh=a.refresh)
    if a.all:
        print(f"\n  {'해변':<12}{'최근접 부이':<14}{'종류':<12}{'거리':>8}   판단")
        print("  " + "─" * 60)
        for k, sp in SPOTS.items():
            b = nearest_for(sp, n=1)
            if b:
                b = b[0]
                print(f"  {k:<12}{b['name'][:12]:<14}{b['kind']:<12}{b['km']:>6.1f}km   {verdict(b['km'])}")
        print("\n  20km 이내면 실측으로 쓸 만하고, 50km를 넘으면 해변 상태와 무관해집니다.\n")
        return
    sp = find_spot(a.spot)
    print(f"\n  {sp['name']} 인근 파고 관측지점\n")
    for b in nearest_for(sp, n=6):
        print(f"   {b['km']:>6.1f}km  {b['id']:<8}{b['name'][:12]:<14}{b['kind']}")
    print()


# ---------------------------------------------------------------- 실황
def fmt(v, u=""):
    return f"{v:.1f}{u}" if isinstance(v, float) else "–"


def cmd_obs(a):
    tm = a.tm or obs_time()
    rows = sea_obs(tm)
    if not rows:
        raise SystemExit("관측 자료가 비었습니다. `buoy.py raw sea_obs` 로 확인하세요.")
    ids = None
    if a.near:
        sp = find_spot(a.near)
        ids = {b["id"] for b in nearest_for(sp, n=8)}
        print(f"\n  {sp['name']} 인근 관측", end="")
    else:
        print("\n  전체 관측", end="")
    print(f"  ({tm[:4]}-{tm[4:6]}-{tm[6:8]} {tm[8:10]}:{tm[10:]})\n")
    print(f"  {'지점':<8}{'이름':<14}{'종류':<12}{'파고':>7}{'수온':>7}{'풍속':>7}{'풍향':>7}")
    print("  " + "─" * 64)
    shown = 0
    for r in rows:
        if ids and r["STN_ID"] not in ids:
            continue
        if not ids and r["TP"] not in WAVE_TP:
            continue
        print(f"  {r['STN_ID']:<8}{r['STN_KO'][:12]:<14}{TP_NAME.get(r['TP'], r['TP']):<12}"
              f"{fmt(f(r,'WH'),'m'):>7}{fmt(f(r,'TW'),'℃'):>7}"
              f"{fmt(f(r,'WS')):>7}{fmt(f(r,'WD'),'°'):>7}")
        shown += 1
    if not shown:
        print("  해당 지점 자료가 없습니다 (결측이거나 관측 시각이 다름).")
    print("\n  출처: 기상청 API허브\n")


def cmd_detail(a):
    txt = fetch("kma_buoy.php", {"tm": a.tm or obs_time(), "stn": a.stn, "help": 0})
    rows = parse_csvish(txt, BUOY_COLS)
    if not rows:
        print("자료가 비었습니다. 해양기상부이(TP=B) 번호인지 확인하고,")
        print("컬럼이 어긋난 것 같으면 `buoy.py raw buoy --stn " + str(a.stn) + "` 원문을 보세요.")
        return
    for r in rows:
        print(f"\n  지점 {r.get('STN','?')}   관측 {r.get('TM','?')}")
        print(f"   유의파고 {fmt(f(r,'WH_SIG'),'m')}   최대 {fmt(f(r,'WH_MAX'),'m')}"
              f"   평균 {fmt(f(r,'WH_AVE'),'m')}")
        print(f"   파주기   {fmt(f(r,'WP'),'s')}   파향 {fmt(f(r,'WO'),'°')}")
        print(f"   바람     {fmt(f(r,'WS1'),'m/s')} {fmt(f(r,'WD1'),'°')}"
              f"   돌풍 {fmt(f(r,'WS1_GST'),'m/s')}")
        print(f"   수온     {fmt(f(r,'TW'),'℃')}   기온 {fmt(f(r,'TA'),'℃')}")
    print("\n  출처: 기상청 API허브\n")


# ---------------------------------------------------------------- 모델 대조
def model_at(lat, lon, tm):
    p = {"latitude": lat, "longitude": lon, "timezone": "Asia/Seoul", "forecast_days": 2,
         "hourly": "wave_height,wave_period,sea_surface_temperature"}
    req = urllib.request.Request(f"{MARINE}?{urllib.parse.urlencode(p)}",
                                 headers={"User-Agent": "buoy-cli/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
    except Exception:
        return None
    h = d.get("hourly", {})
    stamp = f"{tm[:4]}-{tm[4:6]}-{tm[6:8]}T{tm[8:10]}:00"
    times = h.get("time") or []
    if stamp not in times:
        return None
    i = times.index(stamp)
    g = lambda k: (h.get(k) or [None] * len(times))[i]
    return {"wave": g("wave_height"), "period": g("wave_period"), "sst": g("sea_surface_temperature")}


def do_compare(spot_q, tm=None):
    sp = find_spot(spot_q)
    tm = tm or obs_time()
    rows = {r["STN_ID"]: r for r in sea_obs(tm)}
    out = []
    for b in nearest_for(sp, n=5):
        r = rows.get(b["id"])
        if not r:
            continue
        obs_wh, obs_tw = f(r, "WH"), f(r, "TW")
        if obs_wh is None:
            continue
        m = model_at(b["lat"], b["lon"], tm)
        if not m or m["wave"] is None:
            continue
        out.append({"b": b, "obs": obs_wh, "mdl": m["wave"],
                    "ratio": m["wave"] / obs_wh if obs_wh > 0.05 else None,
                    "obs_tw": obs_tw, "mdl_tw": m["sst"]})
    return sp, tm, out


def cmd_compare(a):
    sp, tm, out = do_compare(a.spot, a.tm)
    print(f"\n  {sp['name']} — 실측과 모델 대조  ({tm[:4]}-{tm[4:6]}-{tm[6:8]} {tm[8:10]}:{tm[10:]})\n")
    if not out:
        print("  대조 가능한 지점이 없습니다. 결측이거나 부이가 너무 멉니다.\n")
        return
    print(f"  {'부이':<14}{'거리':>8}{'실측파고':>10}{'모델파고':>10}{'배율':>8}{'실측수온':>10}{'모델수온':>10}")
    print("  " + "─" * 72)
    for o in out:
        ratio = f"{o['ratio']:.2f}x" if o["ratio"] else "–"
        tw_o = f"{o['obs_tw']:.1f}℃" if o["obs_tw"] is not None else "–"
        tw_m = f"{o['mdl_tw']:.1f}℃" if o["mdl_tw"] is not None else "–"
        print(f"  {o['b']['name'][:12]:<14}{o['b']['km']:>6.1f}km"
              f"{o['obs']:>9.2f}m{o['mdl']:>9.2f}m{ratio:>8}{tw_o:>10}{tw_m:>10}")
    rs = [o["ratio"] for o in out if o["ratio"]]
    if rs:
        avg = sum(rs) / len(rs)
        note = ("모델이 실측보다 큽니다. 예보 파고를 낮춰 보세요." if avg > 1.15 else
                "모델이 실측보다 작습니다. 예보 파고를 높여 보세요." if avg < 0.85 else
                "모델과 실측이 대체로 일치합니다.")
        print(f"\n  평균 배율 {avg:.2f}x — {note}")
        print(f"  보정계수 {1/avg:.2f} (모델 파고 × 이 값)")
    print("\n  ※ 한 시점의 값입니다. `log` 로 며칠 쌓아야 경향이 보입니다.")
    print("  출처: 기상청 API허브\n")


def cmd_log(a):
    sp, tm, out = do_compare(a.spot, a.tm)
    if not out:
        raise SystemExit("기록할 대조 결과가 없습니다.")
    new = not os.path.exists(CALIB)
    with open(CALIB, "a", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        if new:
            w.writerow(["기록시각", "관측시각", "해변", "부이", "거리km",
                        "실측파고m", "모델파고m", "배율", "실측수온", "모델수온"])
        for o in out:
            w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), tm, sp["name"],
                        o["b"]["name"], f"{o['b']['km']:.1f}",
                        f"{o['obs']:.2f}", f"{o['mdl']:.2f}",
                        f"{o['ratio']:.3f}" if o["ratio"] else "",
                        f"{o['obs_tw']:.1f}" if o["obs_tw"] is not None else "",
                        f"{o['mdl_tw']:.1f}" if o["mdl_tw"] is not None else ""])
    print(f"{len(out)}건을 {CALIB} 에 기록했습니다.")


def cmd_raw(a):
    p = {"tm": a.tm or obs_time(), "stn": a.stn or 0, "help": 1}
    print(fetch("sea_obs.php" if a.what == "sea_obs" else "kma_buoy.php", p))


def main():
    ap = argparse.ArgumentParser(description="기상청 부이 실측 조회 및 모델 대조")
    s = ap.add_subparsers(dest="cmd", required=True)

    p = s.add_parser("nearest", help="해변 최근접 부이")
    p.add_argument("spot", nargs="?"); p.add_argument("--all", action="store_true")
    p.add_argument("--refresh", action="store_true", help="지점 캐시 갱신")
    p.set_defaults(fn=cmd_nearest)

    p = s.add_parser("obs", help="실황 조회")
    p.add_argument("--near"); p.add_argument("--tm"); p.set_defaults(fn=cmd_obs)

    p = s.add_parser("detail", help="해양기상부이 상세")
    p.add_argument("stn"); p.add_argument("--tm"); p.set_defaults(fn=cmd_detail)

    p = s.add_parser("compare", help="실측 vs 모델")
    p.add_argument("spot"); p.add_argument("--tm"); p.set_defaults(fn=cmd_compare)

    p = s.add_parser("log", help="대조 결과 누적")
    p.add_argument("spot"); p.add_argument("--tm"); p.set_defaults(fn=cmd_log)

    p = s.add_parser("raw", help="응답 원문")
    p.add_argument("what", choices=["sea_obs", "buoy"])
    p.add_argument("--stn"); p.add_argument("--tm"); p.set_defaults(fn=cmd_raw)

    a = ap.parse_args(); a.fn(a)


if __name__ == "__main__":
    main()
