from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "Tuesday KYC Platform"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"
    ENABLE_DOCS: bool = True

    # Which browsers may call this API. "*" is any page anywhere, which is what a
    # verification journey served from its own domain needs, and it is the default.
    # A comma separated list of origins narrows it without a release.
    #
    # Credentials stay off whatever this says: no browser allows them with a
    # wildcard, and nothing here authenticates with a cookie. A call is authorised
    # by the session token in its URL or by an API key in a header, and neither is
    # something a browser attaches on its own.
    CORS_ORIGINS: str = "*"

    # Base URL for session links (e.g. http://localhost:4501). If empty, request base_url is used.
    VERIFY_BASE_URL: str = ""

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

    # How long a verification link works for, in minutes. Shufti's default is
    # 60 and a client can override it per request with ttl.
    SESSION_TTL_MINUTES: int = 60
    # The longest a client may ask for: 30 days. A link that lives for weeks is
    # a link that can be forwarded, so this is a ceiling rather than an
    # encouragement, and it is a setting so it can be lowered without a release.
    MAX_SESSION_TTL_MINUTES: int = 60 * 24 * 30

    # Evidence lives in Google Drive, reached with a service account key, the way
    # our database backups do. Its two interesting fields:
    GOOGLE_SERVICE_ACCOUNT_EMAIL: str = ""
    GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY: str = ""
    # Or the whole key file, if a deployment would rather mount one.
    GOOGLE_SERVICE_ACCOUNT_JSON: str = ""

    # The folder evidence goes in: the id from its Drive URL. On a Shared Drive,
    # which is the only place a service account can write.
    GOOGLE_DRIVE_FOLDER_ID: str = ""

    # The platform's own folder inside it. Everything is written under this one,
    # a subfolder per verification named by our verification id.
    GOOGLE_DRIVE_ROOT_FOLDER: str = "KYC Verifications"

    # Where files go while Drive is not configured: a folder on this machine.
    # That is how the tests and a developer's laptop run, and it is not a
    # setting anybody turns on in production, where the Drive values above
    # decide it.
    EVIDENCE_DIR: str = "var/evidence"

    # Set to a folder to keep the frames of each face check, for tuning thresholds.
    # Off by default: these are photos of people's faces.
    DEBUG_FRAMES_DIR: str = ""

    DATABASE_URL: str = "postgresql+psycopg://kyc:kyc@localhost:5432/kyc"

    # Signing key for dashboard sessions. The default is for a developer's
    # machine only: check_configuration() refuses to start with it anywhere else.
    JWT_SECRET: str = "development-secret-not-for-anywhere-else"
    ACCESS_TOKEN_MINUTES: int = 60 * 12
    # How long a password reset link is good for. Long enough to find the email,
    # short enough that a forwarded one is useless tomorrow.
    PASSWORD_RESET_MINUTES: int = 30
    # The same rule tuesday-backend applies (UpdatePasscodeDto): 8 to 16
    # characters, no required mixture of character classes. Keeping one rule
    # across our services means a person is not told different things by
    # different parts of Tuesday.
    MIN_PASSWORD_LENGTH: int = 8
    MAX_PASSWORD_LENGTH: int = 16
    # The longest an API key may be asked to live.
    MAX_API_KEY_DAYS: int = 365

    # Email is not wired up yet (see app/services/email.py). While it is off and
    # the environment is development, a password reset returns its token in the
    # response so the flow can be tested; anywhere else that is refused at
    # start-up.
    EMAIL_ENABLED: bool = False
    DASHBOARD_URL: str = "http://localhost:3000"
    REDIS_URL: str = "redis://localhost:6379/0"


settings = Settings()
