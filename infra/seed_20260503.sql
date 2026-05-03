-- RainCall 2026-05-03 대구 라이온즈파크 경기 결과
-- 실행 방법: psql $DATABASE_URL -f seed_20260503.sql

-- 1) game_results 저장
INSERT INTO game_results (
    stadium_id,
    game_date,
    home_team,
    away_team,
    scheduled_time,
    status,
    accumulated_rain,
    peak_wind,
    season
) VALUES (
    'lions_park',
    '2026-05-03',
    '삼성 라이온즈',
    '한화 이글스',
    '14:00',
    '진행',
    15.0,
    8.0,
    2026
)
ON CONFLICT (stadium_id, game_date, home_team, away_team)
DO UPDATE SET
    status           = EXCLUDED.status,
    accumulated_rain = EXCLUDED.accumulated_rain,
    peak_wind        = EXCLUDED.peak_wind;

-- 2) predictions 테이블 actual_result 업데이트
--    (lions_park, 2026-05-03 예측 레코드가 있을 경우)
UPDATE predictions
SET    actual_result = 'played'
WHERE  game_id IN (
           SELECT id FROM games
           WHERE  stadium = '대구'
           AND    game_date = '2026-05-03'
       );

-- 결과 확인
SELECT
    gr.game_date,
    gr.stadium_id,
    gr.home_team || ' vs ' || gr.away_team   AS match,
    gr.scheduled_time,
    gr.status,
    gr.accumulated_rain  AS rain_mm,
    gr.peak_wind         AS wind_ms,
    p.cancel_probability AS predicted_prob,
    p.actual_result
FROM  game_results gr
LEFT JOIN games      g  ON g.stadium = '대구'  AND g.game_date = gr.game_date
LEFT JOIN predictions p  ON p.game_id = g.id
WHERE gr.game_date = '2026-05-03'
  AND gr.stadium_id = 'lions_park';
