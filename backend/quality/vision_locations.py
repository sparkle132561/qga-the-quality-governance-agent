"""仅在规范化图片OCR中寻找位置依据；模型坐标不能兜底为取证坐标。"""
import math
import re
import unicodedata

from rapidfuzz.distance import Levenshtein


def text_key(text):
    return "".join(char for char in unicodedata.normalize("NFKC", text).casefold()
                   if char.isalnum())


def locate_fields(reading, units, width, height):
    lines = []
    for index, unit in enumerate(units):
        points = unit.get("location", {}).get("box", [])
        if len(points) != 4 or not text_key(unit["text"]):
            continue
        if not all(len(p) == 2 and all(math.isfinite(v) for v in p) for p in points):
            continue
        bounds = [min(p[0] for p in points), min(p[1] for p in points),
                  max(p[0] for p in points), max(p[1] for p in points)]
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            continue
        lines.append({"index": index, "text": unit["text"], "box": bounds, "points": points})
    lines.sort(key=lambda line: (line["box"][1], line["box"][0]))
    windows = []
    for start, first in enumerate(lines):
        chain = [first]
        windows.append(chain[:])
        cursor = start
        for _ in range(5):
            current = chain[-1]["box"]
            following = []
            for position in range(cursor + 1, len(lines)):
                box = lines[position]["box"]
                overlap = max(0, min(current[2], box[2]) - max(current[0], box[0]))
                min_width = min(current[2] - current[0], box[2] - box[0])
                height_limit = 1.5 * max(current[3] - current[1], box[3] - box[1])
                if (box[1] > current[1] and box[1] - current[3] <= height_limit
                        and overlap / min_width >= .5):
                    following.append((box[1], abs(box[0] - current[0]), position))
            if not following:
                break
            cursor = min(following)[2]
            chain.append(lines[cursor])
            windows.append(chain[:])
    results = []
    for field in reading.fields:
        identifier = field.name in {"model", "certificate_number", "barcode"}
        def key(text):
            if identifier:
                return "".join(unicodedata.normalize("NFKC", text).split())
            return text_key(text)
        query = key(field.quote)
        numeric = re.findall(r"[-+±]?\d+(?:[.,]\d+)?",
                             unicodedata.normalize("NFKC", field.quote))
        matches = []
        if query and field.certainty == "clear":
            for window in windows:
                actual_text = " ".join(line["text"] for line in window)
                if re.findall(r"[-+±]?\d+(?:[.,]\d+)?",
                              unicodedata.normalize("NFKC", actual_text)) != numeric:
                    continue
                actual = key(actual_text)
                exact = query == actual
                approximate = (field.name in {"brand", "manufacturer", "product_name",
                                               "net_quantity"} and len(query) >= 8
                               and Levenshtein.normalized_similarity(query, actual) >= .9)
                if exact or approximate:
                    matches.append((exact, window))
        exact_matches = [match for match in matches if match[0]]
        matches = exact_matches or matches
        if len(matches) != 1:
            results.append({"status": "needs_manual_location", "box": None,
                            "reason": "ambiguous_ocr_matches" if matches else "no_ocr_match",
                            "unit_indices": [], "source": "canonical_image_ocr"})
            continue
        exact, window = matches[0]
        points = [point for line in window for point in line["points"]]
        box = [max(0, math.floor(min(p[0] for p in points) * 1000 / width)),
               max(0, math.floor(min(p[1] for p in points) * 1000 / height)),
               min(1000, math.ceil(max(p[0] for p in points) * 1000 / width)),
               min(1000, math.ceil(max(p[1] for p in points) * 1000 / height))]
        if not (box[0] < box[2] and box[1] < box[3]):
            results.append({"status": "needs_manual_location", "box": None,
                            "reason": "invalid_ocr_region", "unit_indices": [],
                            "source": "canonical_image_ocr"})
            continue
        results.append({"status": "ocr_exact" if exact else "ocr_approximate", "box": box,
                        "source": "canonical_image_ocr",
                        "unit_indices": [x["index"] for x in window],
                        "polygons_pixels": [line["points"] for line in window],
                        "ocr_text": "\n".join(line["text"] for line in window),
                        "authority": "candidate_location_requires_human_review"})
    return results
