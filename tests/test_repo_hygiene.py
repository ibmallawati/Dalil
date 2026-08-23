"""
Repository invariants that only break on the deployment host.

This project is developed on Windows and deployed to Linux, and the two
disagree about what a legal filename is. That disagreement is invisible
locally: everything works, commits fine, pushes fine, and then the deploy
fails during `git clone` — before a single line of the app has run.
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Linux caps each path COMPONENT at 255 bytes. Windows caps it at 255 UTF-16
# code units, which for Arabic is 255 characters — twice as generous, since
# Arabic is two bytes per letter in UTF-8.
LINUX_NAME_LIMIT = 255


def tracked_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO, capture_output=True, check=True,
    ).stdout
    return [p.decode("utf-8") for p in out.split(b"\0") if p]


def test_no_tracked_filename_exceeds_the_linux_limit():
    """
    Every committed filename must survive a clone on Linux.

    This failed for real on 23 Aug 2026: 18 OCR files carried long Arabic
    names, one of them 819 bytes, and Render's deploy died with

        error: unable to create file data/pdf_ocr/...: File name too long
        fatal: unable to checkout working tree

    Nothing was wrong with the app. The clone simply could not write the
    file, and every retry then failed differently because the half-checked-
    out directory was no longer empty.
    """
    offenders = []
    for path in tracked_files():
        for part in path.split("/"):
            size = len(part.encode("utf-8"))
            if size > LINUX_NAME_LIMIT:
                offenders.append((size, len(part), path))
                break

    assert not offenders, (
        "These tracked names are too long to check out on Linux "
        f"(limit {LINUX_NAME_LIMIT} bytes):\n"
        + "\n".join(
            f"  {size} bytes / {chars} chars — {path[:100]}"
            for size, chars, path in sorted(offenders, reverse=True)
        )
        + "\n\nArabic costs two bytes per letter in UTF-8, so a name that is "
          "legal on Windows can be double the length on the deploy host. "
          "Shorten it, or untrack the file if it is build-time material."
    )


def test_no_tracked_path_exceeds_a_safe_total_length():
    """
    The whole path matters too, not just each component. 4096 is the Linux
    ceiling; 200 is a margin that also survives a deep checkout directory
    like Render's /opt/render/project/src.
    """
    long_paths = [p for p in tracked_files() if len(p.encode("utf-8")) > 200]
    assert not long_paths, (
        "Tracked paths over 200 bytes, which risks the host's own prefix "
        "pushing them past the limit:\n" + "\n".join(f"  {p}" for p in long_paths)
    )
