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
    # Matching runs on two ONNX files from InsightFace's buffalo_l pack: SCRFD
    # for detection and ArcFace for the embedding. See README for the download.
    FACE_DETECTION_MODEL: str = "models/det_10g.onnx"
    FACE_RECOGNITION_MODEL: str = "models/w600k_r50.onnx"
    # -1 runs on CPU. A GPU index (0, 1, ...) uses CUDA, which needs
    # onnxruntime-gpu installed in place of onnxruntime.
    FACE_MODEL_DEVICE_ID: int = -1

    # How long a scanned document stays available for the face step (in memory).
    SESSION_TTL_MINUTES: int = 30

    # Set to a folder to keep the frames of each face check, for tuning thresholds.
    # Off by default: these are photos of people's faces.
    DEBUG_FRAMES_DIR: str = ""

    DATABASE_URL: str = "postgresql+psycopg://kyc:kyc@localhost:5432/kyc"
    REDIS_URL: str = "redis://localhost:6379/0"


settings = Settings()
