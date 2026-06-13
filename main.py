from __future__ import annotations

import os
import re
import asyncio
import httpx
from datetime import datetime, date
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from bs4 import BeautifulSoup
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="직관예보 API", description="KBO 야구 경기 우천취소 예측 서비스 — 기상청 초단기실황 + KBO 공식 취소 기준 기반")

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

app.mount("/static", StaticFiles(directory="static"), name="static")

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
# 구장 키 → slug 역매핑
STADIUM_KEY_TO_SLUG: Dict[str, str] = {v: k for k, v in STADIUM_SLUGS.items()}

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
    models: Optional[Dict[str, Any]] = None   # 모델별 수치 + 앙상블
    model_agreement: Optional[str] = None     # 높음 | 보통 | 낮음

class GameStatusInfo(BaseModel):
    status: str                      # scheduled | in_progress | canceled | completed
    home: str
    away: str
    reason: Optional[str] = None     # 취소 사유
    score: Optional[str] = None      # "원정:홈" 형식 (e.g. "3:2")
    status_detail: Optional[str] = None  # 진행중 상세 (이닝 정보 등)

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


# ── KBO 경기 일정 ────────────────────────────────────────────────────
# 기본값: 오늘(2026-05-03) KBO 공식 확인 대진
MOCK_GAMES = [
    {"id": "G001", "home": "LG",   "away": "NC",   "stadium": "잠실", "time": "14:00"},
    {"id": "G002", "home": "SSG",  "away": "롯데", "stadium": "인천", "time": "14:00"},
    {"id": "G003", "home": "삼성", "away": "한화", "stadium": "대구", "time": "14:00"},
    {"id": "G004", "home": "KIA",  "away": "KT",   "stadium": "광주", "time": "14:00"},
    {"id": "G005", "home": "키움", "away": "두산", "stadium": "고척", "time": "14:00"},
]

# 당일 스케줄 캐시 (날짜 바뀌면 자동 무효화)
_schedule_cache: Dict[str, list] = {}

# KBO 영문 사이트 구장명 → STADIUMS 키 매핑
_KBO_STADIUM_MAP = {
    "jamsil":      "잠실",
    "munhak":      "인천",
    "incheon":     "인천",
    "daegu":       "대구",
    "lions":       "대구",
    "gwangju":     "광주",
    "gocheok":     "고척",
    "gocheoksky":  "고척",
    "suwon":       "수원",
    "changwon":    "창원",
    "sajik":       "부산",
    "daejeon":     "대전",
}

# KBO 영문팀명 → 한국어
_KBO_TEAM_MAP = {
    "LG Twins":        "LG",
    "Doosan Bears":    "두산",
    "KIA Tigers":      "KIA",
    "Samsung Lions":   "삼성",
    "SSG Landers":     "SSG",
    "Lotte Giants":    "롯데",
    "Hanwha Eagles":   "한화",
    "NC Dinos":        "NC",
    "KT Wiz":          "KT",
    "Kiwoom Heroes":   "키움",
}


async def fetch_kbo_schedule_live(game_date: Optional[date] = None) -> List[dict]:
    """KBO 공식 영문 사이트에서 당일 경기 일정 스크래핑.
    파싱 실패 시 MOCK_GAMES 반환.
    """
    target = game_date or date.today()
    date_str = target.strftime("%Y%m%d")

    # 당일 캐시 확인
    if date_str in _schedule_cache:
        return _schedule_cache[date_str]

    url = f"https://eng.koreabaseball.com/Schedule/DailySchedule.aspx?gameDate={date_str}"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            res = await client.get(url)
            res.raise_for_status()

        soup = BeautifulSoup(res.text, "lxml")
        rows = soup.select("table.tbl tr")[1:]  # 헤더 제외

        games = []
        for i, row in enumerate(rows):
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) < 4:
                continue
            # 열 순서: Time | Away | Home | Stadium ...
            time_str = cells[0][:5] if cells[0] else "14:00"
            away_en  = cells[1]
            home_en  = cells[2]
            stad_raw = cells[3].lower() if len(cells) > 3 else ""

            home = _KBO_TEAM_MAP.get(home_en, home_en)
            away = _KBO_TEAM_MAP.get(away_en, away_en)
            stadium = next(
                (v for k, v in _KBO_STADIUM_MAP.items() if k in stad_raw),
                stad_raw
            )
            games.append({
                "id":      f"G{i+1:03d}",
                "home":    home,
                "away":    away,
                "stadium": stadium,
                "time":    time_str,
            })

        if games:
            _schedule_cache[date_str] = games
            return games

    except Exception:
        pass  # 스크래핑 실패 시 MOCK_GAMES 반환

    return MOCK_GAMES

# 네이버 스포츠 홈팀명 → slug 매핑
_HOME_TEAM_TO_SLUG: Dict[str, str] = {
    "LG":   "jamsil",
    "두산": "jamsil",
    "SSG":  "incheon",
    "롯데": "busan",
    "삼성": "lions_park",
    "한화": "daejeon",
    "KIA":  "gwangju",
    "KT":   "suwon",
    "키움": "gocheok",
    "NC":   "changwon",
}

_status_cache: Dict[str, Any] = {}
_STATUS_CACHE_TTL = 300  # 5분

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


async def fetch_open_meteo(lat: float, lon: float, model: str) -> Dict[str, Any]:
    """Open-Meteo API 호출 (무료, API 키 불필요).
    현재 시각 기준 1시간 강수량·풍속·강수확률 반환.
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=precipitation,windspeed_10m,precipitation_probability"
        f"&forecast_days=1&timezone=Asia%2FSeoul&models={model}"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.get(url)
        res.raise_for_status()
        data = res.json()

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    precip = hourly.get("precipitation", [])
    wind = hourly.get("windspeed_10m", [])
    precip_prob = hourly.get("precipitation_probability", [])

    # 현재 시각과 가장 가까운 인덱스 찾기
    now_str = datetime.now().strftime("%Y-%m-%dT%H:00")
    idx = times.index(now_str) if now_str in times else 0

    return {
        "precipitation": float(precip[idx]) if precip else 0.0,
        "windspeed":     float(wind[idx])   if wind   else 0.0,
        "precip_prob":   int(precip_prob[idx]) if precip_prob else 0,
    }


def calc_ensemble(kma_rain: float, gfs_rain: float, ecmwf_rain: float) -> float:
    """가중 평균 앙상블: 기상청 50% + GFS 20% + ECMWF 30%
    GFS는 한반도 강수를 과대 추정하는 경향이 있어 가중치 낮춤."""
    return kma_rain * 0.5 + gfs_rain * 0.2 + ecmwf_rain * 0.3


def model_agreement_level(kma_rain: float, gfs_rain: float, ecmwf_rain: float) -> str:
    """모델 간 강수량 표준편차 기반 동의도 계산."""
    values = [kma_rain, gfs_rain, ecmwf_rain]
    mean = sum(values) / 3
    std = (sum((v - mean) ** 2 for v in values) / 3) ** 0.5
    if std <= 0.5:
        return "높음"
    if std <= 2.0:
        return "보통"
    return "낮음"


async def fetch_naver_game_status(game_date: Optional[date] = None) -> Dict[str, GameStatusInfo]:
    """네이버 스포츠 API로 KBO 경기 실시간 상태 조회. 5분 캐시."""
    now_ts = datetime.now().timestamp()
    if _status_cache and now_ts - _status_cache.get("cached_at", 0) < _STATUS_CACHE_TTL:
        return _status_cache["data"]

    target = game_date or date.today()
    date_str = target.strftime("%Y%m%d")
    url = (
        "https://api-gw.sports.naver.com/schedule/games"
        f"?fields=basic,homeTeam,awayTeam&gameDate={date_str}&leagueId=kbo"
    )
    result: Dict[str, GameStatusInfo] = {}
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as client:
            res = await client.get(url)
            res.raise_for_status()
            payload = res.json()

        games = payload.get("result", {}).get("games", [])
        for g in games:
            home = g.get("homeTeamName", "")
            away = g.get("awayTeamName", "")
            slug = _HOME_TEAM_TO_SLUG.get(home)
            if not slug:
                continue

            status_code = g.get("statusCode", "BEFORE")
            is_cancel = g.get("cancel", False)
            status_info = g.get("statusInfo", "")

            if is_cancel or status_code == "CANCEL":
                status = "canceled"
            elif status_code == "RESULT":
                status = "completed"
            elif status_code in ("LIVE", "PLAYING"):
                status = "in_progress"
            else:
                status = "scheduled"

            h_score = g.get("homeTeamScore")
            a_score = g.get("awayTeamScore")
            score = None
            if status in ("completed", "in_progress") and h_score is not None and a_score is not None:
                score = f"{a_score}:{h_score}"

            result[slug] = GameStatusInfo(
                status=status,
                home=home,
                away=away,
                reason=status_info if status == "canceled" else None,
                score=score,
                status_detail=status_info if status == "in_progress" else None,
            )
    except Exception:
        pass  # 조회 실패 시 빈 dict → 프론트에서 scheduled로 폴백

    _status_cache["data"] = result
    _status_cache["cached_at"] = now_ts
    return result


# ── API 엔드포인트 ──────────────────────────────────────────────────
@app.get("/")
def root():
    return {"service": "직관예보", "mode": "mock" if MOCK_MODE else "live", "docs": "/docs"}


@app.get("/choir", response_class=FileResponse)
def choir_finder_app():
    """동작소년소녀합창단 합창대회 찾기 앱."""
    return FileResponse("static/choir.html")


@app.get("/api/games")
async def list_games():
    games = await fetch_kbo_schedule_live()
    return {"date": date.today().isoformat(), "games": games, "source": "live" if games is not MOCK_GAMES else "mock"}


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
    lat, lon = stadium["lat"], stadium["lon"]

    # KMA + GFS + ECMWF 병렬 호출
    kma_result, gfs_result, ecmwf_result = await asyncio.gather(
        fetch_ultra_realtime(stadium["nx"], stadium["ny"]),
        fetch_open_meteo(lat, lon, "gfs_seamless"),
        fetch_open_meteo(lat, lon, "ecmwf_ifs04"),
        return_exceptions=True,
    )

    if isinstance(kma_result, Exception):
        raise HTTPException(502, f"기상청 초단기실황 오류: {kma_result}")
    weather, base_time = kma_result

    kma_rain = weather.precipitation
    gfs_rain  = gfs_result["precipitation"]  if not isinstance(gfs_result,  Exception) else kma_rain
    ecmwf_rain = ecmwf_result["precipitation"] if not isinstance(ecmwf_result, Exception) else kma_rain

    ensemble_rain = round(calc_ensemble(kma_rain, gfs_rain, ecmwf_rain), 2)
    agreement = model_agreement_level(kma_rain, gfs_rain, ecmwf_rain)

    # 앙상블 강수량으로 취소 확률 재계산
    ensemble_weather = weather.model_copy(update={"precipitation": ensemble_rain})
    prob, reasons = kbo_cancellation_engine(ensemble_weather, is_dome)

    # 각 모델의 취소 판정 (강수 1mm 이상 = 취소 위험)
    cancel_votes = sum(1 for r in [kma_rain, gfs_rain, ecmwf_rain] if r >= 1.0)

    # GFS 과대 추정 감지: std ≥ 1.5mm 이고 GFS가 가장 높을 때
    _vals = [kma_rain, gfs_rain, ecmwf_rain]
    _mean = sum(_vals) / 3
    _std  = (sum((v - _mean) ** 2 for v in _vals) / 3) ** 0.5
    gfs_outlier = _std >= 1.5 and gfs_rain == max(_vals)

    models_data: Dict[str, Any] = {
        "kma":          {"rain_1h": round(kma_rain,   2), "weight": "50%"},
        "gfs":          {"rain_1h": round(gfs_rain,   2), "weight": "20%"},
        "ecmwf":        {"rain_1h": round(ecmwf_rain, 2), "weight": "30%"},
        "ensemble":     ensemble_rain,
        "cancel_votes": cancel_votes,
        "gfs_outlier":  gfs_outlier,
    }

    return RealtimePrediction(
        slug=slug,
        stadium_key=stadium_key,
        stadium_name=stadium["name"],
        is_dome=is_dome,
        cancel_probability=round(prob, 3),
        risk_level=risk_label(prob),
        reasons=reasons,
        weather=ensemble_weather,
        data_source="KMA+GFS+ECMWF 앙상블",
        kma_base_time=base_time,
        fetched_at=datetime.now().isoformat(timespec="seconds"),
        models=models_data,
        model_agreement=agreement,
    )


@app.get("/api/predict-all")
async def predict_all():
    games = await fetch_kbo_schedule_live()
    results = []
    for game in games:
        stadium_key = game["stadium"]
        is_dome = stadium_key == "고척"
        if MOCK_MODE or not KMA_API_KEY:
            weather = MOCK_WEATHER.get(stadium_key, WeatherData(
                precipitation=0, precipitation_prob=0, sky_condition=1,
                thunder=False, wind_speed=0, temperature=20, humidity=50
            ))
        else:
            stadium = STADIUMS.get(stadium_key)
            if not stadium:
                continue
            weather, _ = await fetch_kma_weather(stadium["nx"], stadium["ny"])
        prob, reasons = kbo_cancellation_engine(weather, is_dome)
        results.append({
            "game":              f"{game['away']} @ {game['home']}",
            "game_id":           game["id"],
            "slug":              STADIUM_KEY_TO_SLUG.get(stadium_key, ""),
            "stadium":           STADIUMS.get(stadium_key, {}).get("name", stadium_key),
            "game_time":         game["time"],
            "cancel_probability": round(prob, 3),
            "risk_level":        risk_label(prob),
            "reasons":           reasons,
            "weather":           weather,
            "is_dome":           is_dome,
        })
    return {"date": date.today().isoformat(), "predictions": results}


@app.get("/api/games/today/status")
async def games_today_status():
    """네이버 스포츠 API 기반 오늘 경기 실시간 상태 조회 (5분 캐시)."""
    statuses = await fetch_naver_game_status()
    return {"date": date.today().isoformat(), "statuses": statuses}


# ── 합창대회 찾기 서비스 ────────────────────────────────────────────

DONGJAK_CHOIR_PROFILE: Dict[str, Any] = {
    "name": "동작소년소녀합창단",
    "description": "서울 동작구 소재 어린이·청소년 합창단",
    "type": "소년소녀합창단",
    "eligible_categories": ["소년소녀합창", "어린이합창", "청소년합창", "학생합창"],
    "region": "서울",
    "district": "동작구",
    "age_groups": ["초등학생", "중학생"],
}

CHOIR_COMPETITIONS_DB: List[Dict[str, Any]] = [
    {
        "id": "CC001",
        "name": "제30회 전국소년소녀합창경연대회",
        "organizer": "사단법인 한국합창단연합회",
        "categories": ["소년소녀합창", "어린이합창"],
        "eligible_age_groups": ["초등학생", "중학생"],
        "region": "전국",
        "venue": "세종문화회관 대극장 (서울)",
        "application_start": "2026-03-10",
        "application_deadline": "2026-05-15",
        "competition_date": "2026-07-12",
        "prize_summary": "대상 교육부장관상 + 500만원",
        "prizes": {
            "대상 (1팀)": "교육부장관상 + 500만원",
            "최우수상 (2팀)": "문화체육관광부장관상 + 300만원",
            "우수상 (3팀)": "300만원",
            "장려상 (5팀)": "100만원",
        },
        "description": (
            "전국 소년소녀합창단이 참여하는 국내 최대 규모의 어린이·청소년 합창경연대회입니다. "
            "초등학생 및 중학생으로 구성된 합창단이 참가 가능하며, 자유곡 및 지정곡 부문으로 나뉩니다."
        ),
        "requirements": "초등학생 또는 중학생 단원으로 구성된 합창단 (20인 이상 80인 이하)",
        "notes": "신청 마감 완료 — 대회는 2026년 7월 12일 예정",
        "tags": ["소년소녀", "어린이", "전국규모", "서울"],
        "contact": "한국합창단연합회 사무국",
    },
    {
        "id": "CC002",
        "name": "서울시 합창경연대회 어린이부",
        "organizer": "서울특별시 · 서울시합창연합회",
        "categories": ["소년소녀합창", "어린이합창"],
        "eligible_age_groups": ["초등학생", "중학생"],
        "region": "서울",
        "venue": "예술의전당 콘서트홀 (서울 서초구)",
        "application_start": "2026-06-01",
        "application_deadline": "2026-07-31",
        "competition_date": "2026-09-13",
        "prize_summary": "서울시장상 + 300만원",
        "prizes": {
            "대상 (1팀)": "서울시장상 + 300만원",
            "최우수상 (2팀)": "서울시의회의장상 + 200만원",
            "우수상 (3팀)": "서울시교육감상 + 150만원",
            "장려상 (5팀)": "서울시합창연합회장상 + 50만원",
        },
        "description": (
            "서울시민 합창문화 발전을 위해 매년 개최되는 서울시 공식 합창경연대회입니다. "
            "어린이부는 서울시 소재 또는 서울시민으로 구성된 어린이 합창단이 참가 가능합니다. "
            "동작구 소재 합창단으로서 참가 자격이 충분합니다."
        ),
        "requirements": "서울시 소재 또는 서울시민으로 구성된 어린이 합창단 (15인 이상)",
        "notes": None,
        "tags": ["서울", "어린이", "서울시장상", "지역대회"],
        "contact": "서울시합창연합회",
    },
    {
        "id": "CC003",
        "name": "전국교육감기 학생합창경연대회",
        "organizer": "전국시도교육감협의회 · 교육부",
        "categories": ["학생합창", "어린이합창", "청소년합창"],
        "eligible_age_groups": ["초등학생", "중학생", "고등학생"],
        "region": "전국",
        "venue": "국립합창단 공연장 (서울 서초구)",
        "application_start": "2026-07-01",
        "application_deadline": "2026-08-20",
        "competition_date": "2026-10-17",
        "prize_summary": "교육부장관상 + 500만원",
        "prizes": {
            "대상 (1팀)": "교육부장관상 + 500만원",
            "금상 (3팀)": "전국시도교육감협의회장상 + 300만원",
            "은상 (5팀)": "200만원",
            "동상 (7팀)": "100만원",
        },
        "description": (
            "교육부와 전국시도교육감협의회가 주최하는 학생 합창 경연대회입니다. "
            "학교 소속이 아닌 지역 소년소녀합창단도 해당 지역 교육청 추천을 받아 참가 가능합니다. "
            "서울시교육청을 통해 추천을 받으면 참가 자격이 생깁니다."
        ),
        "requirements": "초·중·고 학생 단원으로 구성된 합창단 (25인 이상), 교육청 추천 필요",
        "notes": "서울시교육청 추천 공문 필요 — 사전에 서울시교육청 예술체육교육과에 문의 요망",
        "tags": ["학생", "교육부", "전국규모", "교육청추천"],
        "contact": "교육부 예술교육팀",
    },
    {
        "id": "CC004",
        "name": "대한합창연합회 전국합창경연대회 어린이·청소년부",
        "organizer": "사단법인 대한합창연합회",
        "categories": ["소년소녀합창", "어린이합창", "청소년합창"],
        "eligible_age_groups": ["초등학생", "중학생", "고등학생"],
        "region": "전국",
        "venue": "성남아트센터 오페라하우스 (경기 성남)",
        "application_start": "2026-05-01",
        "application_deadline": "2026-06-30",
        "competition_date": "2026-08-22",
        "prize_summary": "대통령상 (종합대상) + 1,000만원",
        "prizes": {
            "대통령상 종합대상 (1팀)": "1,000만원",
            "어린이·청소년부 대상 (1팀)": "문화체육관광부장관상 + 500만원",
            "어린이·청소년부 최우수상 (2팀)": "300만원",
            "어린이·청소년부 우수상 (3팀)": "200만원",
            "장려상": "100만원",
        },
        "description": (
            "국내 최권위 합창경연대회 중 하나로, 어린이·청소년부를 별도로 운영합니다. "
            "소년소녀합창단, 학교합창단 등 어린이·청소년 합창단이라면 누구나 참가 가능합니다. "
            "자유곡 2곡(한국 창작곡 1곡 포함) 연주 필수."
        ),
        "requirements": "초·중학생 단원으로 구성 (10인 이상), 학교 외 지역합창단 참가 가능",
        "notes": "마감 17일 전 — 서둘러 지원 필요",
        "tags": ["대통령상", "권위", "전국규모", "어린이청소년부"],
        "contact": "사단법인 대한합창연합회",
    },
    {
        "id": "CC005",
        "name": "한국합창예술축제 어린이합창 부문",
        "organizer": "국립합창단 · 문화체육관광부",
        "categories": ["소년소녀합창", "어린이합창"],
        "eligible_age_groups": ["초등학생", "중학생"],
        "region": "전국",
        "venue": "국립합창단 공연장 (서울 서초구)",
        "application_start": "2026-08-01",
        "application_deadline": "2026-09-15",
        "competition_date": "2026-11-07",
        "prize_summary": "문화체육관광부장관상 + 300만원",
        "prizes": {
            "대상 (1팀)": "문화체육관광부장관상 + 300만원",
            "최우수상 (2팀)": "국립합창단장상 + 200만원",
            "우수상 (3팀)": "100만원",
            "참가상 (전팀)": "기념패",
        },
        "description": (
            "국립합창단이 주관하는 합창예술 축제의 일환으로 진행되는 경연대회입니다. "
            "경연 외에도 국립합창단 단원과의 합동 연습 및 교육 프로그램이 포함되어 있어 "
            "단원들에게 귀한 성장 기회가 됩니다."
        ),
        "requirements": "초등학생·중학생으로 구성된 합창단 (15인 이상 60인 이하)",
        "notes": None,
        "tags": ["국립합창단", "교육프로그램", "어린이", "전국"],
        "contact": "국립합창단 기획팀",
    },
    {
        "id": "CC006",
        "name": "World Choir Games 어린이합창 부문",
        "organizer": "Interkultur Foundation",
        "categories": ["어린이합창", "소년소녀합창"],
        "eligible_age_groups": ["초등학생", "중학생"],
        "region": "국제",
        "venue": "미정 (격년 개최, 해외 도시)",
        "application_start": "2026-01-01",
        "application_deadline": "2026-03-31",
        "competition_date": "2026-07-04",
        "prize_summary": "금·은·동 메달 + 국제 인증서",
        "prizes": {
            "Gold Diploma": "95점 이상",
            "Silver Diploma": "85~94점",
            "Bronze Diploma": "75~84점",
            "Participation Diploma": "74점 이하",
        },
        "description": (
            "세계 최대 규모의 합창 올림픽으로 100개국 이상에서 합창단이 참가합니다. "
            "어린이합창(Children's Choirs) 부문이 별도로 운영됩니다. "
            "경쟁 부문과 비경쟁 부문(Festival) 중 선택 가능. "
            "참가비 및 해외 여행 경비가 발생하므로 사전 예산 계획이 필수입니다."
        ),
        "requirements": "12세 이하(또는 중학생 이하) 단원으로 구성, 인원 제한 없음",
        "notes": "신청 마감 완료 — 다음 회차(2028년) 일정 참고",
        "tags": ["국제대회", "세계", "어린이", "해외참가"],
        "contact": "Interkultur Foundation (독일)",
    },
    {
        "id": "CC007",
        "name": "경기도 합창경연대회 어린이·청소년부",
        "organizer": "경기도 · 경기도합창연합회",
        "categories": ["어린이합창", "청소년합창"],
        "eligible_age_groups": ["초등학생", "중학생", "고등학생"],
        "region": "경기도",
        "venue": "경기아트센터 대극장 (경기 수원)",
        "application_start": "2026-06-15",
        "application_deadline": "2026-07-20",
        "competition_date": "2026-09-05",
        "prize_summary": "경기도지사상 + 300만원",
        "prizes": {
            "대상 (1팀)": "경기도지사상 + 300만원",
            "최우수상 (2팀)": "경기도의회의장상 + 200만원",
            "우수상 (3팀)": "경기도합창연합회장상 + 100만원",
            "장려상 (5팀)": "50만원",
        },
        "description": (
            "경기도 및 인근 수도권 합창단이 참가하는 지역 합창경연대회입니다. "
            "서울 소재 합창단도 참가 자격이 있으며(거주지 제한 없음), "
            "어린이·청소년부가 별도로 운영됩니다."
        ),
        "requirements": "초·중학생으로 구성된 합창단 (15인 이상), 지역 제한 없음",
        "notes": "경기도 대회이나 서울 합창단도 참가 가능 — 참가 자격 사전 확인 권장",
        "tags": ["경기도", "수도권", "어린이청소년", "지역대회"],
        "contact": "경기도합창연합회",
    },
    {
        "id": "CC008",
        "name": "KBS 전국어린이합창제",
        "organizer": "KBS한국방송 · KBS음악단",
        "categories": ["어린이합창"],
        "eligible_age_groups": ["초등학생"],
        "region": "전국",
        "venue": "KBS홀 (서울 여의도)",
        "application_start": "2026-02-01",
        "application_deadline": "2026-04-10",
        "competition_date": "2026-05-05",
        "prize_summary": "KBS 사장상 + 방송 출연 기회",
        "prizes": {
            "대상 (1팀)": "KBS 사장상 + 방송 출연 + 200만원",
            "최우수상 (2팀)": "KBS음악단장상 + 150만원",
            "우수상 (3팀)": "100만원",
        },
        "description": (
            "KBS가 어린이날을 기념하여 개최하는 전국 어린이합창제입니다. "
            "초등학생으로만 구성된 합창단이 참가하며, 수상팀은 KBS 방송 출연 기회가 주어집니다. "
            "2026년 대회는 이미 종료되었으며, 2027년 대회를 준비하시기 바랍니다."
        ),
        "requirements": "초등학생 단원으로만 구성 (중학생 포함 불가), 20인 이상",
        "notes": "초등학생 전용 — 중학생 단원이 있는 경우 참가 불가",
        "tags": ["KBS", "방송출연", "어린이날", "초등학생전용"],
        "contact": "KBS음악단",
    },
    {
        "id": "CC009",
        "name": "서울교육감기 학생합창경연대회",
        "organizer": "서울특별시교육청",
        "categories": ["학생합창", "어린이합창", "청소년합창"],
        "eligible_age_groups": ["초등학생", "중학생", "고등학생"],
        "region": "서울",
        "venue": "세종문화회관 체임버홀 (서울 종로구)",
        "application_start": "2026-07-15",
        "application_deadline": "2026-08-31",
        "competition_date": "2026-10-24",
        "prize_summary": "서울시교육감상 + 200만원",
        "prizes": {
            "대상 (1팀)": "서울시교육감상 + 200만원",
            "최우수상 (2팀)": "150만원",
            "우수상 (3팀)": "100만원",
            "장려상 (5팀)": "50만원",
        },
        "description": (
            "서울시교육청이 주최하는 서울 학생 합창 경연대회입니다. "
            "서울 소재 학교 합창단 뿐만 아니라 서울시교육청에 등록된 "
            "청소년 합창단(지역 소년소녀합창단 포함)도 참가 가능합니다. "
            "동작구 소재 합창단으로서 적극 추천하는 대회입니다."
        ),
        "requirements": "서울 소재 학교 또는 서울시교육청 등록 합창단, 초·중·고 학생 단원",
        "notes": None,
        "tags": ["서울", "교육청", "학생", "서울교육감상"],
        "contact": "서울시교육청 예술체육교육과",
    },
    {
        "id": "CC010",
        "name": "한국합창지휘자협회 전국합창경연대회 어린이부",
        "organizer": "한국합창지휘자협회",
        "categories": ["소년소녀합창", "어린이합창", "청소년합창"],
        "eligible_age_groups": ["초등학생", "중학생"],
        "region": "전국",
        "venue": "예술의전당 IBK챔버홀 (서울 서초구)",
        "application_start": "2026-09-01",
        "application_deadline": "2026-10-15",
        "competition_date": "2026-11-28",
        "prize_summary": "한국합창지휘자협회장상 + 300만원",
        "prizes": {
            "대상 (1팀)": "300만원",
            "최우수상 (2팀)": "200만원",
            "우수상 (3팀)": "100만원",
            "장려상 (5팀)": "50만원",
        },
        "description": (
            "한국합창지휘자협회가 주최하는 연말 합창경연대회입니다. "
            "어린이부, 청소년부, 성인부로 나뉘어 진행되며, "
            "전문 지휘자와 합창 전문가들로 구성된 심사위원단이 심사합니다."
        ),
        "requirements": "초·중학생으로 구성된 합창단 (12인 이상), 전국 어디서나 참가 가능",
        "notes": None,
        "tags": ["지휘자협회", "전국", "연말대회", "전문심사"],
        "contact": "한국합창지휘자협회",
    },
]


def _deadline_status(deadline_str: str, comp_date_str: str) -> str:
    today = date.today()
    deadline = date.fromisoformat(deadline_str)
    comp_date = date.fromisoformat(comp_date_str)
    if comp_date < today:
        return "종료"
    if deadline < today:
        return "마감"
    days_left = (deadline - today).days
    if days_left <= 14:
        return "마감임박"
    return "지원가능"


def _check_eligibility(comp: Dict[str, Any]) -> Dict[str, Any]:
    profile_cats = set(DONGJAK_CHOIR_PROFILE["eligible_categories"])
    profile_ages = set(DONGJAK_CHOIR_PROFILE["age_groups"])

    cat_match = set(comp["categories"]) & profile_cats
    age_match = set(comp["eligible_age_groups"]) & profile_ages

    eligible = bool(cat_match) and bool(age_match)

    notes: List[str] = []
    if cat_match:
        notes.append(f"참가 부문 적합 ({', '.join(sorted(cat_match))})")
    else:
        notes.append(f"참가 부문 불일치 (대회 부문: {', '.join(sorted(comp['categories']))})")

    if age_match:
        notes.append(f"연령대 적합 ({', '.join(sorted(age_match))})")
    else:
        notes.append(f"연령대 불일치 (대회 대상: {', '.join(sorted(comp['eligible_age_groups']))})")

    region = comp["region"]
    if region in ("전국", "국제"):
        notes.append(f"지역 제한 없음 ({region})")
        region_score = 20
    elif region == "서울":
        notes.append("서울 소재 대회 (동작구 포함)")
        region_score = 20
    elif region == "경기도":
        notes.append("경기도 대회 — 서울 합창단 참가 가능 여부 사전 확인 권장")
        region_score = 10
    else:
        notes.append(f"지역 제한 있음: {region}")
        region_score = 0

    if comp.get("notes"):
        notes.append(f"참고: {comp['notes']}")

    score = (40 if cat_match else 0) + (30 if age_match else 0) + region_score
    d_status = _deadline_status(comp["application_deadline"], comp["competition_date"])
    if d_status == "지원가능":
        score += 10
    elif d_status == "마감임박":
        score += 5

    return {
        "is_eligible": eligible,
        "eligibility_notes": notes,
        "match_score": min(score, 100),
        "deadline_status": d_status,
        "days_until_deadline": max(
            0, (date.fromisoformat(comp["application_deadline"]) - date.today()).days
        ),
    }


@app.get("/api/choir/profile")
def get_choir_profile():
    """동작소년소녀합창단 프로필 조회."""
    return DONGJAK_CHOIR_PROFILE


@app.get("/api/choir/competitions")
def list_choir_competitions(
    eligible_only: bool = False,
    region: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
):
    """합창대회 목록 조회.

    - eligible_only=true: 동작소년소녀합창단 참가 가능 대회만 반환
    - region: 서울 | 경기도 | 전국 | 국제
    - category: 소년소녀합창 | 어린이합창 | 청소년합창 | 학생합창
    - status: 지원가능 | 마감임박 | 마감 | 종료
    """
    STATUS_ORDER = {"지원가능": 0, "마감임박": 1, "마감": 2, "종료": 3}
    results = []

    for comp in CHOIR_COMPETITIONS_DB:
        elig = _check_eligibility(comp)

        if eligible_only and not elig["is_eligible"]:
            continue
        if region and comp["region"] not in (region, "전국", "국제"):
            continue
        if category and not any(category in c for c in comp["categories"]):
            continue
        if status and elig["deadline_status"] != status:
            continue

        results.append({**comp, **elig})

    results.sort(key=lambda x: (
        0 if x["is_eligible"] else 1,
        STATUS_ORDER.get(x["deadline_status"], 9),
        x["application_deadline"],
    ))

    eligible_count = sum(1 for r in results if r["is_eligible"])
    now_open = sum(
        1 for r in results
        if r["is_eligible"] and r["deadline_status"] in ("지원가능", "마감임박")
    )
    return {
        "choir": DONGJAK_CHOIR_PROFILE["name"],
        "as_of": date.today().isoformat(),
        "total": len(results),
        "eligible_count": eligible_count,
        "now_open_count": now_open,
        "competitions": results,
    }


@app.get("/api/choir/competitions/{competition_id}")
def get_choir_competition(competition_id: str):
    """특정 합창대회 상세 정보 + 동작소년소녀합창단 참가 적합 여부."""
    comp = next((c for c in CHOIR_COMPETITIONS_DB if c["id"] == competition_id), None)
    if not comp:
        raise HTTPException(404, f"대회 ID '{competition_id}'를 찾을 수 없습니다.")
    return {**comp, **_check_eligibility(comp)}
