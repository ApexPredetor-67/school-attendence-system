import os
from pathlib import Path
import cv2
import numpy as np

DEFAULT_TOLERANCE = 0.48

try:
    import face_recognition
    _FACE_REC_AVAILABLE = True
except ImportError:
    face_recognition = None
    _FACE_REC_AVAILABLE = False


def face_rec_available():
    return _FACE_REC_AVAILABLE


def _face_locations(rgb_image, upsample=1):
    if not _FACE_REC_AVAILABLE:
        raise RuntimeError("face_recognition is not installed on this server. Deploy on Render with Python 3.12 to enable face features.")
    return face_recognition.face_locations(rgb_image, model="hog", number_of_times_to_upsample=upsample)


def image_quality(frame, location):
    top, right, bottom, left = location
    h, w = frame.shape[:2]
    width, height = right - left, bottom - top
    if width < max(110, int(w * 0.20)) or height < max(110, int(h * 0.20)):
        return False, "Move closer so the face fills more of the frame"
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    brightness = float(np.mean(gray))
    if brightness < 50:
        return False, "Image is too dark"
    if brightness > 220:
        return False, "Image is too bright"
    blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if blur_score < 45:
        return False, "Image is too blurry; hold still"
    if left < 3 or top < 3 or right > w - 3 or bottom > h - 3:
        return False, "Keep the whole face inside the camera frame"
    return True, "OK"


def get_face_encodings(user_folder):
    if not _FACE_REC_AVAILABLE:
        return []
    encodings = []
    folder = Path(user_folder)
    if not folder.is_dir():
        return encodings
    for path in sorted(folder.glob("*.jpg")):
        try:
            bgr = cv2.imread(str(path))
            if bgr is None:
                continue
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            locations = _face_locations(rgb, upsample=1)
            if len(locations) != 1:
                continue
            ok, _ = image_quality(bgr, locations[0])
            if not ok:
                continue
            found = face_recognition.face_encodings(rgb, locations, num_jitters=4, model="small")
            if found:
                encodings.append(found[0])
        except Exception:
            continue
    return encodings


def train_folder(folder, output_file):
    encodings = get_face_encodings(str(folder))
    if not encodings:
        return False, 0
    np.save(output_file, np.asarray(encodings, dtype=np.float64))
    return True, len(encodings)


def recognize_faces(frame, upsample=1):
    if not _FACE_REC_AVAILABLE:
        raise RuntimeError("face_recognition is not installed on this server.")
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    locations = _face_locations(rgb, upsample=upsample)
    encodings = face_recognition.face_encodings(rgb, locations, num_jitters=2, model="small")
    return locations, encodings


def _user_score(known_encodings, test_encoding):
    if not _FACE_REC_AVAILABLE:
        return None
    distances = face_recognition.face_distance(known_encodings, test_encoding)
    if len(distances) == 0:
        return None
    ordered = np.sort(distances)
    top = ordered[:min(5, len(ordered))]
    return float(top[0] * 0.45 + np.mean(top) * 0.35 + np.median(top) * 0.20)


def best_match_for_encoding(test_encoding, known_users, tolerance=DEFAULT_TOLERANCE):
    if not _FACE_REC_AVAILABLE:
        return None, None, None
    candidates = []
    for user, path in known_users:
        if not os.path.exists(path):
            continue
        try:
            enc = np.load(path, allow_pickle=False)
            if enc.size == 0:
                continue
            if enc.ndim == 1:
                enc = np.expand_dims(enc, 0)
            score = _user_score(enc, test_encoding)
            if score is not None:
                candidates.append((score, user))
        except Exception:
            continue
    if not candidates:
        return None, None, None
    candidates.sort(key=lambda x: x[0])
    best_score, best_user = candidates[0]
    second_score = candidates[1][0] if len(candidates) > 1 else None
    margin_ok = second_score is None or (second_score - best_score) >= 0.035
    if best_score > tolerance or not margin_ok:
        return None, None, second_score
    return best_user, float(best_score), second_score
