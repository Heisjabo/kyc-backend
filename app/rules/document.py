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

# Guiding a live document capture (pipeline/document_probe.py)
#
# The page draws a rectangle, crops each camera frame to it and sends only that,
# so the server judges exactly what would be captured. These say when the card is
# worth capturing. They are deliberately a little stricter than the thresholds
# the finished photo is judged by, because a person holding a card can fix the
# problem in a second, and a manual capture button is always available anyway.
LIVE_ANALYSIS_WIDTH = 640

# Measured on a real driving licence at this width, progressively blurred: with
# no blur it scores 1307 and reads 24 lines; at 97 everything still reads; at 37
# the line count drops; at 20 the country line is lost and the document would be
# refused. The gate sits between the last good reading and the first bad one.
LIVE_MIN_BLUR = 80.0

# Measured: a well lit card reads 122 to 181. Below 80 the print stops resolving,
# above 235 the highlights are clipped.
LIVE_MIN_BRIGHTNESS = 80.0
LIVE_MAX_BRIGHTNESS = 235.0

# Glare is the enemy of a laminated card: a reflection erases whatever is under
# it. Measured on good captures: 0.00 to 0.05 per cent of pixels blown out.
LIVE_MAX_GLARE_RATIO = 0.015

# The portrait has to be visible, and big enough to be worth matching against.
# This doubles as the framing check: a card that fills the rectangle carries a
# large portrait, and a card held too far away carries a small one. Measured as a
# share of the frame's height: 34 per cent on a card filling the frame, 14.5 per
# cent on a card photographed from across a table.
LIVE_MIN_PORTRAIT_RATIO = 0.20

# How much of the rectangle the card covers. Reported to the page as a reading,
# but not enforced: it is measured from edges, which fade when a frame is blurred
# or dark, so a genuine focus problem would be reported as a framing problem. The
# portrait ratio above says the same thing and survives both.
LIVE_MIN_CARD_FILL = 0.55

# Is there a document in view at all, or is the camera looking at a face, a hand
# or a desk? A card carries printed lines of text; nothing else in front of a
# webcam does. Measured as the number of line-shaped blobs at the analysis width:
# real cards 26 (blurred) to 49, faces in the same frame 7 to 12.
LIVE_MIN_TEXT_LINES = 18

# Consecutive good frames before the capture is taken by itself. One good frame
# can be luck between two shaky ones.
LIVE_READY_FRAMES = 3

# Back side analysis
# A back side should not have a portrait photo. A portrait >= 12% is considered a front-facing portrait.
LIVE_BACK_MAX_PORTRAIT_RATIO = 0.12
BACK_SIDE_PORTRAIT_MAX_RATIO = 0.12
IMAGE_DUPLICATE_SIMILARITY_THRESHOLD = 0.88
