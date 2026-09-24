from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "Tuesday KYC Platform"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"
    ENABLE_DOCS: bool = True

    # Downloaded once from the OpenCV model zoo; see README.
    FACE_DETECTOR_MODEL: str = "models/face_detection_yunet_2023mar.onnx"
    # MediaPipe's 478-point face mesh bundle, used for liveness (head pose and
    # planarity). Run through TFLite directly; see README for the download.
    FACE_MESH_MODEL: str = "models/face_landmarker.task"
    # Any DeepFace model; each carries its own tuned threshold, read at runtime.
    # Measured on two real people's Rwandan IDs: Facenet512 put the two different
    # people at 0.548 (below its 0.700 line) while SFace put them at 0.482 (above
    # its 0.407 line, i.e. it called two strangers the same person). Facenet512 is
    # slower on CPU, 170 ms a frame against 20 ms, and worth it.
    FACE_RECOGNITION_MODEL: str = "Facenet512"

    # How long a scanned document stays available for the face step (in memory).
    SESSION_TTL_MINUTES: int = 30

    # Set to a folder to keep the frames of each face check, for tuning thresholds.
    # Off by default: these are photos of people's faces.
    DEBUG_FRAMES_DIR: str = ""

    DATABASE_URL: str = "postgresql+psycopg://kyc:kyc@localhost:5432/kyc"
    REDIS_URL: str = "redis://localhost:6379/0"


settings = Settings()
