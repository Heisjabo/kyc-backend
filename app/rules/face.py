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

# Matching: SCRFD detection, InsightFace alignment, ArcFace embeddings
# (engines/recognition.py). Detection and alignment are the model pack's own, so
# the document portrait and the camera frame are prepared identically.
DETECT_SIZE = (640, 640)   # what SCRFD resizes to; the pack's own default
DETECT_THRESHOLD = 0.6     # below this a detection is not confidently a face

# The pass line. Cosine similarity between two unit-length ArcFace vectors, so it
# runs from -1 to 1 and a score is directly comparable between runs.
#
# InsightFace publishes no threshold for this pack, because the right line
# depends on how the images were taken. Measured here, one person's Rwandan ID
# and driving licence against 20 frames of their own capture, and against six
# strangers' faces:
#
#   same person, ID against own capture        0.46 to 0.58
#   same person, licence against own capture   0.45 to 0.57
#   different people                           0.02 to 0.07
#
# The two groups are half a point apart, so the line sits between them rather
# than inside either: 0.40 leaves the genuine worst case 0.06 of room and is six
# times the highest impostor seen. For comparison, DeepFace's published line for
# its own ArcFace is 0.32, and the FastAPI service this was modelled on uses 0.65
# against its own images, which would have refused every genuine pair measured
# here.
#
# The strangers measured were easy negatives: different people, different
# lighting, different cameras. Hard negatives, people of the same age and
# appearance photographed the same way, are what the line should finally be set
# against, and that calibration set does not exist yet.
MATCH_THRESHOLD = 0.40

# The match is a yes or no. There is no "maybe" band, the way there is none at
# Shufti or Didit: a capture too poor to judge is stopped before matching and the
# person is asked to retake, rather than being scored and half-accepted.

# Frames compared against the document portrait: the sharpest frontal ones.
MATCH_FRAMES = 8
# The score is the mean of the best few, not of all of them. A handful of poor
# frames should not outvote the ones where the face is clearly visible, and one
# lucky frame alone should not carry a decision.
MATCH_TOP_FRAMES = 3

# Only frames where the person faces the camera are compared. The frames from the
# head movements are evidence of liveness, not of identity: a face turned away
# scores lower against a passport-style portrait whoever it belongs to, which
# pushes genuine people under the line. Measured on one capture, the same
# person's ID against their own 20 frames:
#
#   frontal (yaw <= 8, pitch <= 10), 6 frames    0.5242 to 0.5833, mean 0.5673
#   from the movements, 14 frames                0.4612 to 0.5591, mean 0.5239
#
# Frontality is judged in degrees rather than by the liveness step's "centre"
# label, which allows up to TURN_DEGREES (18) of yaw: the two frames that label
# accepted at 12 and 15 degrees scored 0.4842 and 0.5091, the lowest of the six.
FRONTAL_PITCH_DEGREES = 10.0

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


# The liveness clip. Kept so a disputed check can be reviewed by a person later,
# and so a new model can be run over a real capture rather than a test set. A few
# seconds at browser quality is about a megabyte; the ceiling is generous because
# refusing the evidence would be worse than storing a large file.
MAX_VIDEO_BYTES = 25 * 1024 * 1024
ALLOWED_VIDEO_TYPES = {"video/webm", "video/mp4", "video/quicktime"}


# The duplicate face warning: how far above the match line a face has to score
# against somebody else's portrait before it is worth telling a person about.
# Deliberately strict, because at the match line itself this would flag siblings
# and cousins. Not yet measured on real pairs, so it errs towards silence.
DUPLICATE_FACE_MARGIN = 0.15
