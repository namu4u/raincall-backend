CREATE TABLE IF NOT EXISTS games (
    id             VARCHAR(20) PRIMARY KEY,
    game_date      DATE        NOT NULL,
    home_team      VARCHAR(20) NOT NULL,
    away_team      VARCHAR(20) NOT NULL,
    stadium        VARCHAR(30) NOT NULL,
    game_time      TIME        NOT NULL,
    status         VARCHAR(20) DEFAULT 'scheduled',
    created_at     TIMESTAMP   DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS weather_snapshots (
    id                  SERIAL PRIMARY KEY,
    game_id             VARCHAR(20) REFERENCES games(id),
    fetched_at          TIMESTAMP   NOT NULL DEFAULT NOW(),
    precipitation       NUMERIC(5,2),
    precipitation_prob  INT,
    sky_condition       INT,
    thunder             BOOLEAN,
    wind_speed          NUMERIC(5,2),
    temperature         NUMERIC(5,2),
    humidity            INT
);

CREATE TABLE IF NOT EXISTS predictions (
    id                  SERIAL PRIMARY KEY,
    game_id             VARCHAR(20) REFERENCES games(id),
    predicted_at        TIMESTAMP   NOT NULL DEFAULT NOW(),
    cancel_probability  NUMERIC(5,3),
    risk_level          VARCHAR(20),
    reasons             TEXT[],
    actual_result       VARCHAR(20)   -- 실제결과: cancelled / played / postponed
);

CREATE TABLE IF NOT EXISTS game_results (
    id               SERIAL PRIMARY KEY,
    stadium_id       VARCHAR(30)   NOT NULL,
    game_date        DATE          NOT NULL,
    home_team        VARCHAR(30)   NOT NULL,
    away_team        VARCHAR(30)   NOT NULL,
    scheduled_time   TIME          NOT NULL,
    status           VARCHAR(20)   NOT NULL,  -- 진행 / 취소 / 우천취소 / 연기
    accumulated_rain NUMERIC(6,1),            -- 경기 중 누적 강수량 mm
    peak_wind        NUMERIC(5,1),            -- 최대 풍속 m/s
    season           INT           NOT NULL,
    notes            TEXT,
    created_at       TIMESTAMP     DEFAULT NOW(),
    UNIQUE (stadium_id, game_date, home_team, away_team)
);

-- 샘플 경기 데이터
INSERT INTO games (id, game_date, home_team, away_team, stadium, game_time) VALUES
    ('G001', CURRENT_DATE, '두산', 'LG',   '잠실', '18:30'),
    ('G002', CURRENT_DATE, 'KIA', '삼성',  '광주', '18:30'),
    ('G003', CURRENT_DATE, 'SSG', '롯데',  '인천', '18:00'),
    ('G004', CURRENT_DATE, 'NC',  '한화',  '창원', '18:30'),
    ('G005', CURRENT_DATE, 'KT',  '키움',  '수원', '18:30')
ON CONFLICT DO NOTHING;
