from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "Tuesday KYC Platform"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"
    # Interactive API docs. Left unset they are on in development and off everywhere else;
    # set it to true or false to say otherwise.
    ENABLE_DOCS: bool | None = None

    # Which browsers may call this API. "*" is any page anywhere, which is what a
    # verification journey served from its own domain needs, and it is the default.
    # A comma separated list of origins narrows it without a release.
    #
    # Credentials stay off whatever this says: no browser allows them with a
    # wildcard, and nothing here authenticates with a cookie. A call is authorised
    # by the session token in its URL or by an API key in a header, and neither is
    # something a browser attaches on its own.
    CORS_ORIGINS: str = "*"

    # The first-generation endpoints and test pages: /v1/document/scan, /v1/document/frame,
    # /v1/document/birth-certificate, /v1/face/liveness, /v1/face/frame,
    # /v1/verifications/{id}/face, and the pages that drive them. They have no
    # authentication: anyone who can reach them can upload an identity document and get
    # the personal details read from it. Off unless a developer turns it on.
    ENABLE_LEGACY_ENDPOINTS: bool = False

    # A callback URL must resolve to public addresses only. Turn this on to allow
    # localhost and private networks, which a developer's machine and the tests need.
    CALLBACK_ALLOW_PRIVATE_HOSTS: bool = False

    # Nothing here can tell a forged bill or statement from a real one: a PDF made in a
    # few seconds with a PDF library passes every other address check. Until there is a
    # real authenticity check, a proof of address that would otherwise pass goes to a
    # person (decision "review", reason AUTHENTICITY_NOT_VERIFIED). Set this to false
    # only when something else checks the proof.
    ADDRESS_PROOFS_NEED_REVIEW: bool = True

    # Limits on sign-in, registration, password resets and the session endpoints
    # (app/core/ratelimit.py). Turn off only for load tests.
    RATE_LIMITS_ENABLED: bool = True

    # How many times one step (document, address, face, due diligence) may be failed before the
    # verification is sent to a person. Without a cap, someone could keep submitting documents
    # until one passed, and each attempt runs the OCR and face models.
    MAX_STEP_ATTEMPTS: int = 5

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

    # AI-generated image detection (Community Forensics ViT-S, ONNX). Optional:
    # without the file the check is skipped. See README for the download.
    AI_IMAGE_MODEL: str = "models/ai_image_detector.onnx"
    # The probability, from 0 to 1, at which an image counts as AI-generated. At
    # REJECT the step fails; from REVIEW up to REJECT a document goes to review
    # and a face passes with a warning. On public test images real photographs
    # stayed under 0.05 and generated faces scored 0.97 to 1.0, so both lines sit
    # well clear of either; tune them once real submissions have been scored.
    # Where a score becomes a warning. Neither one fails a verification: the
    # detector misses some diffusion images and flags photographs that went through
    # a generative "enhance" app, so it flags rather than refuses.
    AI_IMAGE_SUSPECT_THRESHOLD: float = 0.5
    AI_IMAGE_DETECT_THRESHOLD: float = 0.9
    # What to do with a file whose own credentials say an AI tool made it: "review" sends the
    # step to a person, "flag" records it and carries on, "off" does not look.
    AI_CONTENT_CREDENTIALS_ACTION: str = "review"

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

    # Device and network intelligence (app/services/network.py). The IP
    # databases, the Tor exit list and the hosting ASN list are downloaded here
    # and refreshed in the background; with the downloads off, the report shows
    # only what the browser and the request say.
    NETWORK_DATA_DIR: str = "var/network"
    NETWORK_DATA_DOWNLOAD: bool = True

    # Pangolin AML runs after the identity checks. The token is backend-only.
    YENTE_BASE_URL: str = "https://aml.tuesday.africa"
    YENTE_API_TOKEN: str = ""
    YENTE_SCREENING_DATASET: str = "sanctions"
    YENTE_POSSIBLE_THRESHOLD: float = 0.7
    YENTE_STRONG_THRESHOLD: float = 0.9

    DATABASE_URL: str = "postgresql+psycopg://kyc:kyc@localhost:5432/kyc"

    # Signing key for dashboard sessions. The default is for a developer's
    # machine only: check_configuration() refuses to start with it anywhere else.
    JWT_SECRET: str = "development-secret-not-for-anywhere-else"
    ACCESS_TOKEN_MINUTES: int = 60 * 12
    # How long a password reset link is good for. Long enough to find the email,
    # short enough that a forwarded one is useless tomorrow.
    PASSWORD_RESET_MINUTES: int = 30
    # At least 12 characters and up to 128, so a passphrase fits, and refused if it is on the
    # bundled list of commonly leaked passwords (app/core/passwords.py). No required mixture
    # of character classes.
    MIN_PASSWORD_LENGTH: int = 12
    MAX_PASSWORD_LENGTH: int = 128
    # Sent as Strict-Transport-Security when the request came in over https (behind nginx,
    # X-Forwarded-Proto). A year; 0 turns it off.
    HSTS_SECONDS: int = 31536000
    # The longest an API key may be asked to live.
    MAX_API_KEY_DAYS: int = 365

    # Email is not wired up yet (see app/services/email.py). While it is off and
    # the environment is development, a password reset returns its token in the
    # response so the flow can be tested; anywhere else that is refused at
    # start-up.
    EMAIL_ENABLED: bool = False
    # Outgoing mail over SMTP (any provider offers it). EMAIL_ENABLED turns sending on.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_STARTTLS: bool = True
    EMAIL_FROM: str = "Tuesday KYC <no-reply@tuesday.africa>"

    # Limits on one account.
    MAX_API_KEYS_PER_ACCOUNT: int = 10
    # Verifications one account may open in a day (0 is no limit).
    MAX_VERIFICATIONS_PER_DAY: int = 5000

    # Billing (app/services/billing.py). Usage is always metered. While this is
    # off nothing is refused for want of credit or plan, so clients already
    # live are not cut off the day billing ships; turn it on once every
    # existing client is on the plan they should be.
    BILLING_ENFORCED: bool = False
    # Shared with the Pangolin Sign server, which presents it to reserve and
    # settle Sign requests (/v1/billing/sign/*). Empty turns those endpoints off.
    SIGN_BILLING_SECRET: str = ""
    # The longest side of an uploaded image or PDF page, in pixels, checked before it is decoded.
    MAX_IMAGE_SIDE_PX: int = 8000
    MAX_IMAGE_PIXELS: int = 40_000_000
    MAX_PDF_PAGES: int = 10
    DASHBOARD_URL: str = "http://localhost:3000"
    REDIS_URL: str = "redis://localhost:6379/0"


settings = Settings()
