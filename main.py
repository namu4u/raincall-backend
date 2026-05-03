from __future__ import annotations

import os
import re
import httpx
from datetime import datetime, date
from typing import Optional, List, Dict
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="RainCall API", description="KBO 경기 취소 예측 서비스")

# 추가 허용 오리진은 ALLOWED_ORIGINS 환경변수로 주입 (쉼표 구분)
# 예) ALLOWED_ORIGINS=https://frontend-one-eosin-17.vercel.app,https://raincall.vercel.app
_extra = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
CORS_ORIGINS = ["http://localhost:5173"] + _extra

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=r"https://.*\.vercel\.app",  # Vercel 프리뷰 배포 전체 허용
    allow_methods=["*"],
    allow_headers=["*"],
)

MOCK_MODE = os.getenv("MOCK_MODE", "true").lower() == "true"
KMA_API_KEY = os.getenv("KMA_API_KEY", "")
PORT = int(os.getenv("PORT", "8000"))  # Railway가 $PORT 주입

# ── KBO 구장 위치 정보 ──────────────────────────────────────────────
# KMA 단기예보 발표 시각 (KST, 매 3시간 + 약 10분 지연)
KMA_BASE_TIMES = ["0200", "0500", "0800", "1100", "1400", "1700", "2000", "2300"]

STADIUMS = {
    "잠실": {"name": "잠실야구장", "team": "두산/LG", "nx": 61, "ny": 126, "lat": 37.5120, "lon": 127.0717},
    "고척": {"name": "고척스카이돔", "team": "키움", "nx": 58, "ny": 125, "lat": 37.4982, "lon": 126.8672},
    "수원": {"name": "수원KT위즈파크", "team": "KT", "nx": 60, "ny": 121, "lat": 37.2998, "lon": 127.0100},
    "인천": {"name": "SSG랜더스필드", "team": "SSG", "nx": 54, "ny": 125, "lat": 37.4370, "lon": 126.6933},
    "대전": {"name": "한화생명이글스파크", "team": "한화", "nx": 67, "ny": 100, "lat": 36.3170, "lon": 127.4290},
    "광주": {"name": "광주-기아챔피언스필드", "team": "KIA", "nx": 58, "ny": 74, "lat": 35.1681, "lon": 126.8893},
    "대구": {"name": "라이온즈파크", "team": "삼성", "nx": 89, "ny": 90, "lat": 35.8408, "lon": 128.6811},
    "창원": {"name": "창원NC파크", "team": "NC", "nx": 90, "ny": 77, "lat": 35.2225, "lon": 128.5820},
    "부산": {"name": "사직야구장", "team": "롯데", "nx": 98, "ny": 76, "lat": 35.1944, "lon": 129.0614},
}

# URL slug → STADIUMS 키 매핑
STADIUM_SLUGS: Dict[str, str] = {
    "jamsil":       "잠실",
    "gocheok":      "고척",
    "suwon":        "수원",
    "incheon":      "인천",
    "daejeon":      "대전",
    "gwangju":      "광주",
    "lions_park":   "대구",
    "changwon":     "창원",
    "busan":        "부산",
}

# ── KBO 우천 취소 룰 엔진 ───────────────────────────────────────────
class WeatherData(BaseModel):
    precipitation: float       # 강수량 mm/h
    precipitation_prob: int    # 강수확률 %
    sky_condition: int         # 하늘상태 1=맑음 3=구름많음 4=흐림
    thunder: bool              # 낙뢰 여부
    wind_speed: float          # 풍속 m/s
    temperature: float         # 기온 °C
    humidity: int              # 습도 %

class PredictionResult(BaseModel):
    game_id: str
    stadium: str
    game_time: str
    cancel_probability: float
    risk_level: str            # LOW / MEDIUM / HIGH / VERY_HIGH
    reasons: List[str]
    weather: WeatherData
    is_dome: bool

class RealtimePrediction(BaseModel):
    slug: str
    stadium_key: str
    stadium_name: str
    is_dome: bool
    cancel_probability: float
    risk_level: str
    reasons: List[str]
    weather: WeatherData
    data_source: str           # 호출한 기상청 API 종류
    kma_base_time: str         # 기상청 관측/발표 기준 시각
    fetched_at: str            # ISO8601 조회 시각

def kbo_cancellation_engine(weather: WeatherData, is_dome: bool) -> tuple:
    """KBO 공식 우천취소 기준 기반 취소 확률 계산"""
    if is_dome:
        return 0.0, []

    score = 0.0
    reasons = []

    # 낙뢰: 즉시 경기 중단 사유
    if weather.thunder:
        score += 0.6
        reasons.append("낙뢰 감지 — 즉시 중단 사유")

    # 강수량 기준 (KBO: 1이닝당 강수가 일정 수준 이상이면 콜드게임/취소)
    if weather.precipitation >= 10:
        score += 0.5
        reasons.append(f"강수량 {weather.precipitation}mm/h — 경기 진행 불가 수준")
    elif weather.precipitation >= 5:
        score += 0.35
        reasons.append(f"강수량 {weather.precipitation}mm/h — 높음")
    elif weather.precipitation >= 1:
        score += 0.2
        reasons.append(f"강수량 {weather.precipitation}mm/h — 보통")
    elif weather.precipitation > 0:
        score += 0.05
        reasons.append(f"강수량 {weather.precipitation}mm/h — 낮음")

    # 강수확률
    if weather.precipitation_prob >= 80:
        score += 0.25
        reasons.append(f"강수확률 {weather.precipitation_prob}% — 매우 높음")
    elif weather.precipitation_prob >= 60:
        score += 0.15
        reasons.append(f"강수확률 {weather.precipitation_prob}%")
    elif weather.precipitation_prob >= 40:
        score += 0.07

    # 하늘 상태
    if weather.sky_condition == 4:
        score += 0.05
        reasons.append("하늘 흐림")

    # 강풍 (KBO: 풍속 14m/s 이상 시 경기 중단 고려)
    if weather.wind_speed >= 14:
        score += 0.3
        reasons.append(f"강풍 {weather.wind_speed}m/s — 경기 중단 기준 초과")
    elif weather.wind_speed >= 10:
        score += 0.1
        reasons.append(f"강풍 {weather.wind_speed}m/s")

    return min(score, 1.0), reasons


def risk_label(prob: float) -> str:
    if prob >= 0.7:
        return "VERY_HIGH"
    if prob >= 0.45:
        return "HIGH"
    if prob >= 0.2:
        return "MEDIUM"
    return "LOW"


# ── Mock 데이터 ─────────────────────────────────────────────────────
MOCK_GAMES = [
    {"id": "G001", "home": "두산", "away": "LG", "stadium": "잠실", "time": "18:30"},
    {"id": "G002", "home": "KIA", "away": "삼성", "stadium": "광주", "time": "18:30"},
    {"id": "G003", "home": "SSG", "away": "롯데", "stadium": "인천", "time": "18:00"},
    {"id": "G004", "home": "NC",  "away": "한화", "stadium": "창원", "time": "18:30"},
    {"id": "G005", "home": "KT",  "away": "키움", "stadium": "수원", "time": "18:30"},
]

MOCK_WEATHER: Dict[str, WeatherData] = {
    "잠실":  WeatherData(precipitation=3.5, precipitation_prob=70, sky_condition=4, thunder=False, wind_speed=5.2, temperature=18.0, humidity=85),
    "광주":  WeatherData(precipitation=0.0, precipitation_prob=20, sky_condition=1, thunder=False, wind_speed=2.1, temperature=22.0, humidity=55),
    "인천":  WeatherData(precipitation=8.0, precipitation_prob=90, sky_condition=4, thunder=True,  wind_speed=9.0, temperature=16.0, humidity=92),
    "창원":  WeatherData(precipitation=1.0, precipitation_prob=50, sky_condition=3, thunder=False, wind_speed=4.5, temperature=20.0, humidity=75),
    "수원":  WeatherData(precipitation=0.0, precipitation_prob=10, sky_condition=1, thunder=False, wind_speed=1.8, temperature=21.0, humidity=50),
}


# ── 기상청 초단기실황 API ────────────────────────────────────────────
def _latest_ultra_base_time(now: datetime) -> tuple:
    """초단기실황 기준 시각 계산.
    관측 자료는 매시 정각 기준, 약 45분 후 API 제공.
    Returns: (base_date: str, base_time: str)  예) ("20260503", "1100")
    """
    from datetime import timedelta
    if now.minute >= 45:
        return now.strftime("%Y%m%d"), now.strftime("%H00")
    prev = now - timedelta(hours=1)
    return prev.strftime("%Y%m%d"), prev.strftime("%H00")


async def fetch_ultra_realtime(nx: int, ny: int) -> tuple:
    """기상청 초단기실황(getUltraSrtNcst) 호출.
    Returns: (WeatherData, base_time_str)

    초단기실황 카테고리:
      T1H 기온, RN1 1h강수량, WSD 풍속, VEC 풍향,
      REH 습도, PTY 강수형태(0없음/1비/2비눈/3눈/4소나기),
      UUU·VVV 바람성분  — LGT(낙뢰)는 초단기예보에만 있음
    """
    now = datetime.now()
    base_date, base_time = _latest_ultra_base_time(now)

    url = (
        "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst"
        f"?serviceKey={KMA_API_KEY}&pageNo=1&numOfRows=10&dataType=JSON"
        f"&base_date={base_date}&base_time={base_time}&nx={nx}&ny={ny}"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.get(url)
        res.raise_for_status()
        body = res.json()["response"]
        if body["header"]["resultCode"] != "00":
            raise HTTPException(502, f"기상청 초단기실황 오류: {body['header']['resultMsg']}")
        items = body["body"]["items"]["item"]

    # 초단기실황은 obsrValue 키 사용 (단기예보의 fcstValue와 다름)
    obs = {i["category"]: i["obsrValue"] for i in items}

    pty = int(obs.get("PTY", "0"))          # 강수형태
    rn1 = _parse_pcp(obs.get("RN1", "0"))   # 1시간 강수량

    # sky_condition·precipitation_prob 은 실황에 없으므로 PTY·RN1 에서 파생
    if rn1 > 0:
        precip_prob, sky = 100, 4
    elif pty > 0:
        precip_prob, sky = 80, 4
    else:
        precip_prob, sky = 0, 1

    weather = WeatherData(
        precipitation=rn1,
        precipitation_prob=precip_prob,
        sky_condition=sky,
        thunder=False,   # LGT 없음 — 초단기예보(getUltraSrtFcst)에서만 제공
        wind_speed=float(obs.get("WSD", "0")),
        temperature=float(obs.get("T1H", "20")),
        humidity=int(float(obs.get("REH", "50"))),
    )
    return weather, base_time


# ── 기상청 단기예보 API ─────────────────────────────────────────────
def _latest_base_time(now: datetime) -> tuple:
    """현재 시각 기준 가장 최근 기상청 발표 시각 반환 (date_str, base_time)
    발표는 매 3시간마다, 실제 서비스까지 약 10분 소요."""
    hhmm = now.hour * 100 + now.minute
    available = [int(t) for t in KMA_BASE_TIMES if int(t) + 10 <= hhmm]
    if available:
        base_time = f"{max(available):04d}"
        return now.strftime("%Y%m%d"), base_time
    # 자정 이전 발표(전날 2300) 사용
    from datetime import timedelta
    yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
    return yesterday, "2300"


async def fetch_kma_weather(nx: int, ny: int, forecast_hour: str = "1800") -> tuple:
    """기상청 단기예보 조회.
    Returns: (WeatherData, used_base_time)
    forecast_hour: 예보 시각 우선순위 기준 (기본 "1800", KBO 경기 시간)
    """
    now = datetime.now()
    base_date, base_time = _latest_base_time(now)

    url = (
        "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
        f"?serviceKey={KMA_API_KEY}&pageNo=1&numOfRows=300&dataType=JSON"
        f"&base_date={base_date}&base_time={base_time}&nx={nx}&ny={ny}"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.get(url)
        res.raise_for_status()
        body = res.json()["response"]
        if body["header"]["resultCode"] != "00":
            raise HTTPException(502, f"기상청 API 오류: {body['header']['resultMsg']}")
        items = body["body"]["items"]["item"]

    # forecast_hour에 가장 가까운 예보 시각 선택
    fh = int(forecast_hour)
    candidates = sorted(
        {i["fcstTime"] for i in items},
        key=lambda t: abs(int(t) - fh)
    )
    data: Dict[str, str] = {}
    for slot in candidates:
        slot_data = {i["category"]: i["fcstValue"] for i in items if i["fcstTime"] == slot}
        if slot_data:
            data = slot_data
            break

    weather = WeatherData(
        precipitation=_parse_pcp(data.get("PCP", "0")),
        precipitation_prob=int(data.get("POP", "0")),
        sky_condition=int(data.get("SKY", "1")),
        thunder=float(data.get("LGT", "0")) > 0,
        wind_speed=float(data.get("WSD", "0")),
        temperature=float(data.get("TMP", "20")),
        humidity=int(data.get("REH", "50")),
    )
    return weather, base_time


def _parse_pcp(raw: str) -> float:
    """기상청 강수량 문자열 → float (mm/h)
    예: '강수없음' → 0.0, '1 미만' → 0.5, '1.0mm' → 1.0, '1~4mm' → 4.0, '50mm 이상' → 50.0
    """
    raw = raw.strip()
    if "없음" in raw or raw in ("0", ""):
        return 0.0
    if "미만" in raw:
        return 0.5
    if "이상" in raw:
        m = re.search(r"[\d.]+", raw)
        return float(m.group()) if m else 0.0
    if "~" in raw:
        return float(raw.split("~")[-1].replace("mm", "").strip())
    return float(raw.replace("mm", "").strip() or "0")


# ── API 엔드포인트 ──────────────────────────────────────────────────
@app.get("/")
def root():
    return {"service": "RainCall", "mode": "mock" if MOCK_MODE else "live", "docs": "/docs"}


@app.get("/api/games")
def list_games():
    return {"date": date.today().isoformat(), "games": MOCK_GAMES}


@app.get("/api/weather/{stadium_key}")
async def get_weather(stadium_key: str):
    if stadium_key not in STADIUMS:
        raise HTTPException(404, f"구장 '{stadium_key}' 을 찾을 수 없습니다.")

    if MOCK_MODE or not KMA_API_KEY:
        weather = MOCK_WEATHER.get(stadium_key, WeatherData(
            precipitation=0, precipitation_prob=0, sky_condition=1,
            thunder=False, wind_speed=0, temperature=20, humidity=50
        ))
        return {"stadium": stadium_key, "weather": weather}

    stadium = STADIUMS[stadium_key]
    weather, base_time = await fetch_kma_weather(stadium["nx"], stadium["ny"])
    return {"stadium": stadium_key, "kma_base_time": base_time, "weather": weather}


@app.get("/api/predict/{game_id}", response_model=PredictionResult)
async def predict_cancellation(game_id: str):
    game = next((g for g in MOCK_GAMES if g["id"] == game_id), None)
    if not game:
        raise HTTPException(404, f"경기 '{game_id}' 를 찾을 수 없습니다.")

    stadium_key = game["stadium"]
    is_dome = stadium_key == "고척"

    if MOCK_MODE or not KMA_API_KEY:
        weather = MOCK_WEATHER.get(stadium_key, WeatherData(
            precipitation=0, precipitation_prob=0, sky_condition=1,
            thunder=False, wind_speed=0, temperature=20, humidity=50
        ))
    else:
        stadium = STADIUMS[stadium_key]
        weather, _ = await fetch_kma_weather(stadium["nx"], stadium["ny"])

    prob, reasons = kbo_cancellation_engine(weather, is_dome)

    return PredictionResult(
        game_id=game_id,
        stadium=STADIUMS[stadium_key]["name"],
        game_time=game["time"],
        cancel_probability=round(prob, 3),
        risk_level=risk_label(prob),
        reasons=reasons,
        weather=weather,
        is_dome=is_dome,
    )


@app.get("/predict/realtime/{slug}", response_model=RealtimePrediction)
async def predict_realtime(slug: str):
    """기상청 초단기실황(getUltraSrtNcst)으로 현재 날씨 기반 취소 예측.
    MOCK_MODE 무관하게 항상 실데이터 사용.
    slug: lions_park, jamsil, gocheok, suwon, incheon, daejeon, gwangju, changwon, busan
    """
    if slug not in STADIUM_SLUGS:
        raise HTTPException(
            404,
            f"구장 슬러그 '{slug}' 를 찾을 수 없습니다. "
            f"사용 가능: {', '.join(STADIUM_SLUGS.keys())}"
        )
    if not KMA_API_KEY:
        raise HTTPException(503, "KMA_API_KEY 가 설정되지 않았습니다. .env 파일을 확인하세요.")

    stadium_key = STADIUM_SLUGS[slug]
    stadium = STADIUMS[stadium_key]
    is_dome = stadium_key == "고척"

    weather, base_time = await fetch_ultra_realtime(stadium["nx"], stadium["ny"])
    prob, reasons = kbo_cancellation_engine(weather, is_dome)

    return RealtimePrediction(
        slug=slug,
        stadium_key=stadium_key,
        stadium_name=stadium["name"],
        is_dome=is_dome,
        cancel_probability=round(prob, 3),
        risk_level=risk_label(prob),
        reasons=reasons,
        weather=weather,
        data_source="getUltraSrtNcst",
        kma_base_time=base_time,
        fetched_at=datetime.now().isoformat(timespec="seconds"),
    )


@app.get("/api/predict-all")
async def predict_all():
    results = []
    for game in MOCK_GAMES:
        prediction = await predict_cancellation(game["id"])
        results.append({
            "game": f"{game['away']} @ {game['home']}",
            **prediction.model_dump(),
        })
    return {"date": date.today().isoformat(), "predictions": results}
