# 직관예보 ⚾

오늘 야구 경기, 가도 될까요?  
기상청 초단기실황 API + KBO 공식 취소 기준을 결합해 팬들에게 경기 취소 가능성을 알려줍니다.

## 빠른 시작 (로컬, Mock 모드)

### 백엔드

```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # MOCK_MODE=true 유지
uvicorn main:app --reload
# → http://localhost:8000/docs
```

### 프론트엔드

```bash
cd frontend
npm install
npm run dev
# → http://localhost:5173
```

## 실제 기상청 API 연동

1. [공공데이터포털](https://www.data.go.kr) 회원가입 후 **기상청 단기예보 조회서비스** API 키 발급
2. `backend/.env` 수정:

```env
KMA_API_KEY=발급받은_키
MOCK_MODE=false
```

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET | `/api/games` | 오늘 경기 목록 |
| GET | `/api/weather/{stadium}` | 구장별 날씨 |
| GET | `/api/predict/{game_id}` | 경기 취소 예측 |
| GET | `/api/predict-all` | 전체 경기 예측 |

## KBO 취소 기준 (룰 엔진)

- 낙뢰 감지 → 즉시 취소 사유
- 강수량 ≥ 10mm/h → 경기 진행 불가
- 풍속 ≥ 14m/s → 강풍 취소 기준
- 고척 스카이돔은 날씨 무관 (취소 확률 0%)

## 구장 목록

잠실 · 고척(돔) · 수원 · 인천 · 대전 · 광주 · 대구 · 창원 · 부산
