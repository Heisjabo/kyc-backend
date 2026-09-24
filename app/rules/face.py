"""Thresholds for face detection and the head-turn liveness challenge.

The pose numbers are ratios, not degrees, and need tuning against real captures
from the app's camera guide.
"""

# Detection
FACE_SCORE_THRESHOLD = 0.8

# The portrait on the document is the reference everything is matched against, so
# its size is worth reporting: ICAO 9303 and ISO/IEC 19794-5 ask for roughly 90 px
# between the eyes on a reference photo, and a real ID photograph here gives about
# 196 px of face with the eyes 69 px apart, against 87 px and 31 px for a
# screenshot of an ID. Below these numbers the match is less trustworthy, so it is
# flagged, but it does not stop the check: rejecting documents on size alone turns
# away people whose photo is simply small.
SMALL_DOCUMENT_FACE_HEIGHT = 130
SMALL_DOCUMENT_EYE_DISTANCE = 45
# A face smaller than this share of the frame's short side is too far from the camera.
MIN_FACE_RATIO = 0.15

# The mesh crop: square, with this much margin around the detected face box.
MESH_CROP_MARGIN = 0.45

# Eye openness is no longer a challenge step: at the frame rate a browser can
# manage, a blink falls between frames as often as not. It is kept only to avoid
# choosing a still where the eyes are shut. The line is relative to the person's
# own open measurement, because eye shape varies.
EYES_OPEN_EAR = 0.19
EYES_OPEN_RATIO = 0.70

# A movement counts only when it is held for this many frames running. A single
# frame past the line is ordinary head drift, not someone following the prompt:
# measured on a capture where the person only turned sideways, single frames
# crossed the nod line and were wrongly read as nods.
STEP_FRAMES = 2

# Planarity: how much of the movement a flat-surface transform cannot explain,
# as a percentage of face size. Measured between a frontal frame and a turned one:
#   real face, phone camera, turning 39 to 59 degrees   3.0 to 3.5%
#   real face, laptop webcam                            1.45%
#   a photograph tilted to fake the turn                0.23 to 0.36%
# A printed photo tilted at the camera passes a head-turn check on its own, so
# this is what actually separates a person from a picture. The line sits between
# the two groups rather than close to either: a webcam gives far less depth
# signal than a phone, and 1.2 left almost no room above it.
PLANARITY_MIN = 0.8

# Head pose is in degrees (ISO/IEC 29794-5 expresses face pose as yaw, pitch and
# roll in degrees, and OFIQ, its reference implementation, reports them that way).
# Angles come from fitting a generic 3D head to the detector's landmarks.
#
# Measured on a real phone capture: resting yaw sits around -9 degrees (camera
# position), a deliberate turn reaches 50 degrees or more. Pitch from five
# landmarks is too noisy to use, so nodding is not part of any challenge.
TURN_DEGREES = 18.0
# Nodding moves the head less than turning does, so it needs its own line.
NOD_DEGREES = 12.0
# How close to the resting pose a frame must be to serve as the photo.
FRONTAL_DEGREES = 8.0

# The challenge. "blink" is a step in its own right: a photograph cannot blink,
# and asking for it costs the person a second.
CHALLENGES = {
    "full": ["up", "down", "left", "right"],
    "turns": ["left", "right"],
}
DEFAULT_CHALLENGE = "full"
# Every step must be seen.
ALLOWED_MISSING_DIRECTIONS = 0

# Frames
MIN_FRAMES = 6
MAX_FRAMES = 60
# A face may not jump more than this share of the frame between consecutive captures.
MAX_CENTRE_JUMP_RATIO = 0.35
# ... nor change size by more than this share, which would mean a different capture.
MAX_SIZE_CHANGE_RATIO = 0.6
# Share of frames that must contain exactly one face.
MIN_FACE_FRAME_RATIO = 0.7

# The still we keep: sharpness of the chosen frontal frame.
MIN_CAPTURE_BLUR_SCORE = 60.0

# How the face is prepared before embedding: warped onto a canonical template so
# that a card portrait and a camera frame are framed identically (the alignment
# step described in the MTCNN+FaceNet verification literature).
#
# ALIGN_ZOOM shrinks the template inside the frame, leaving margin around the
# face. The ArcFace template alone (1.00) is tighter than what Facenet512 was
# trained on. Measured on a real pair, genuine against impostor:
#   1.00 -> 0.657 / 0.580   0.85 -> 0.639 / 0.650   0.75 -> 0.684 / 0.596
#   0.65 -> 0.725 / 0.585   0.55 -> 0.714 / 0.693
# 0.65 separates best. Tuned on one pair, so re-measure with the calibration set.
ALIGN_ZOOM = 0.65
ALIGN_MARGIN = 0.25      # only used by the fallback crop
ALIGN_SIZE = (160, 160)  # Facenet512 input size

# Face match. The pass line is NOT set here: it is DeepFace's own tuned threshold
# for the configured model, read at runtime (see engines/recognition.pass_threshold).
# For SFace that is a cosine distance of 0.593, so a similarity of 0.407.
#
# The face match is a yes or no: there is no "maybe" band, the way there is none
# at Shufti or Didit. A capture too poor to judge is stopped before matching and
# the person is asked to retake, rather than being scored and half-accepted.
#
# The line is DeepFace's own tuned threshold for the configured model (0.700 for
# Facenet512), measured here on two real people:
#
#   same person, live face vs own ID      0.71 to 0.75   pass
#   different people, live face vs ID     0.58           fail
#   different people, ID against ID       0.55           fail
#
# It is per-model: SFace put two strangers' IDs at 0.482 against its 0.407 line,
# so switching model means measuring again before trusting it.
MATCH_PASS_MARGIN = 0.0

# Frames compared against the document portrait: the sharpest frontal ones.
MATCH_FRAMES = 8
# The score is the mean of the best few, not of all of them. Measured on the real
# pair above: adding camera grain drops the mean over 8 frames from 0.70 to 0.59
# (a false rejection), while the best 3 only fall from 0.78 to 0.69. A handful of
# poor frames should not outvote the ones where the face is clearly visible.
MATCH_TOP_FRAMES = 3

# Live feedback while the camera guides the person. Thresholds measured on the
# face crop (not the whole frame) from real phone video: blur 40 to 92, brightness
# 83 to 105. A visibly blurred copy of the same frame scores 3 to 5, and a darkened
# one has a brightness of 32.
# These only guide the person while capturing. They must never trap anyone: the
# capture starts after a few seconds whatever they say, and the real decision is
# made on the submitted frames.
LIVE_MIN_BLUR = 15.0
LIVE_MIN_BRIGHTNESS = 45.0
LIVE_MAX_BRIGHTNESS = 200.0
# The face should fill a decent share of the frame's short side before we accept it.
LIVE_MIN_FACE_RATIO = 0.15
LIVE_MAX_FACE_RATIO = 0.90
# Lighting across the face: the darker half must be at least this share of the
# brighter half. Side lighting hides half a face and wrecks the comparison.
# (ISO/IEC 29794-5 calls this illumination uniformity.)
LIVE_MIN_UNIFORMITY = 0.40
# How far the face may sit from the middle of the frame, as a share of the frame.
LIVE_MAX_OFF_CENTRE = 0.35

# The camera is checked before the challenge starts: this many good frames in a
# row before capture begins, so nobody is asked to turn their head in poor light.
READY_FRAMES = 3

# Frames used to learn the person's resting pose before the challenge starts.
CALIBRATION_FRAMES = 4
