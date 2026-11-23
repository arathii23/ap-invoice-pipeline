"""Runs RapidOCR on ONE image in its own short-lived process and prints the page's text lines.

The web server calls this instead of loading the OCR engine itself: OCR memory is handed back to the operating system when the process exits
(a long-lived engine kept growing between pages), and if a small host ever runs out of memory only this process is killed - the run reports
"OCR failed" and the web server, and every visitor's workspace, stay up.

    python ocr_worker.py page.png        # AP_OCR_MAX_SIDE (default 960): the page is shrunk to this many pixels on its long side for text detection
"""
import os, sys


def group_lines(boxes):
    """boxes: [(x, y_center, text)] -> text lines joined with 3 spaces (matches the table parser)."""
    boxes.sort(key=lambda b: (b[1], b[0])); lines = []
    for x, y, t in boxes:
        if lines and abs(lines[-1][0] - y) < 9: lines[-1][1].append((x, t))
        else: lines.append([y, [(x, t)]])
    return "\n".join("   ".join(t for _, t in sorted(l[1])) for l in lines)


def main(path):
    from rapidocr_onnxruntime import RapidOCR
    try: side = int(os.environ.get("AP_OCR_MAX_SIDE", "960"))
    except ValueError: side = 960
    # RapidOCR's own default (grow to a minimum side of 736, never shrink) runs a whole 200-dpi page through the detector: ~900 MB peak. Shrinking: ~300 MB, same fields read.
    res, _ = RapidOCR(det_limit_type="max", det_limit_side_len=side, det_model_path=None)(path)
    sys.stdout.write(group_lines([(b[0][0], (b[0][1] + b[2][1]) / 2, t) for b, t, _ in (res or [])]))


if __name__ == "__main__":
    main(sys.argv[1])
