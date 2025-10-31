import requests
import json
from datetime import datetime

# 체크할 해변 리스트
beaches = [
    {
        "name": "부산 송정해변",
        "url": "https://www.cctv-world.kr/cctv/beach2/songjeong"
    }
]

updated_beaches = []

for b in beaches:
    try:
        r = requests.get(b["url"], timeout=5)
        if r.status_code == 200:
            updated_beaches.append(b)
        else:
            print(f"[오류] {b['name']} 링크 상태: {r.status_code}")
    except Exception as e:
        print(f"[오류] {b['name']} 연결 실패: {e}")

# JSON 생성
output = {
    "last_updated": str(datetime.now()),
    "beaches": updated_beaches
}

with open("beaches.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print("✅ beaches.json 업데이트 완료")
