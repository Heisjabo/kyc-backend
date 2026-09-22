"""Thresholds for the document check. Tune these against real samples."""

# Image quality
QUALITY_ANALYSIS_WIDTH = 1000
MIN_SHORT_SIDE_PX = 600
MIN_LONG_SIDE_PX = 900
MIN_BLUR_SCORE = 100.0
MIN_BRIGHTNESS = 50.0
MAX_BRIGHTNESS = 235.0
GLARE_PIXEL_VALUE = 250
MAX_GLARE_RATIO = 0.05

# Applicant rules
MIN_AGE_YEARS = 18

# Name matching (rapidfuzz ratio, 0 to 100)
NAME_TOKEN_MATCH_SCORE = 85

# Uploads
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
