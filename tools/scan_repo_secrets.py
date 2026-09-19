#!/usr/bin/env python3
"""Fail CI if the working tree carries real network data or a file that must
never be committed.

Why this exists: 2026-09-12, CLAUDE.md rode into a public tag because the
local .gitignore was one commit behind. .gitignore only stops a FUTURE `git
add` - it does nothing about a file that is already tracked, or one added
with -f, or a merge that reintroduces it. This script checks the checked-out
working tree itself, at CI time, which is the one thing .gitignore cannot do.

stdlib only, on purpose - this runs in CI before any dependency is installed.

What it is NOT: a complete secrets scanner. Device names and SSIDs are not
caught by any pattern here - they do not look different from any other
string. Passing this check means "no MAC, private IP, long base64 blob or
forbidden filename was found by these specific rules", not "this tree is
clean". Read the diff too.

A file that is gitignored and untracked is skipped, unless it is also
tracked (force-added past the ignore rule, which is exactly the failure
mode this script exists to catch) - checking a file nobody will ever commit
just makes the local run permanently red for no reason. This changes
nothing in CI: a fresh checkout only ever contains tracked files, so every
file it sees is checked regardless.
"""

import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directories never worth walking into.
SKIP_DIRS = {".git", "__pycache__", "node_modules"}

# Extensions this script reads and scans for MAC/IP/base64 patterns.
SCAN_EXTENSIONS = {".py", ".md", ".json", ".sh", ".example"}

# Filenames that must never reach a commit, anywhere in the tree. This is
# the actual fix for the 2026-09-12 incident: a file that is tracked, or
# force-added past .gitignore, is still caught here because this check reads
# git's own tracked/ignored state, not just the ignore rules on disk - see
# _git_file_status. An untracked file that is ALSO gitignored is skipped;
# see the module docstring.
FORBIDDEN_FILENAMES = {"CLAUDE.md", ".env", "watch_rules.json"}

# --- MAC addresses -----------------------------------------------------------
#
# The IEEE "locally administered" bit (bit 1 of the first octet, i.e.
# first_octet & 0x02) marks an address as software-assigned rather than a
# real vendor allocation - 02:.., 06:.., 0A:.., AA:.. and so on all qualify.
# That is what the project's own placeholders rely on (e.g. aa:bb:cc:dd:ee:ff
# in the README). A MAC that does NOT have that bit set looks like a real,
# vendor-issued address and is flagged, unless it is on the explicit list
# below - named one by one on purpose, not as a second range, so a newly
# invented "safe-looking" prefix cannot slip past unnoticed.
MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\b")
MAC_EXCEPTIONS = {
    "00:00:00:00:00:00",  # watcher.py test_watch_rule sample event
}


def _mac_is_locally_administered(mac):
    first_octet = int(mac.split(":")[0], 16)
    return bool(first_octet & 0x02)


# --- private IPv4 addresses ---------------------------------------------------

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
IP_RE = re.compile(
    r"\b("
    r"10\.%(o)s\.%(o)s\.%(o)s"
    r"|192\.168\.%(o)s\.%(o)s"
    r"|172\.(?:1[6-9]|2\d|3[01])\.%(o)s\.%(o)s"
    r")\b" % {"o": _OCTET}
)

# Explicit, named allow-list of example addresses that already live in the
# repo's own docs and example files - not a wider range, the same principle
# as MAC_EXCEPTIONS. 192.168.1.1 is the default Keenetic LAN address and the
# most common router example; the rest are addresses the README's watcher /
# Home Assistant examples actually use.
IP_EXCEPTIONS = {
    "192.168.1.1",
    "192.168.1.2",
    "192.168.1.41",
    "192.168.1.54",
    "192.168.1.255",
}

# --- base64-like blobs ---------------------------------------------------
#
# Scoped to config EXAMPLE files only (name contains "example"): a real
# source or doc file legitimately contains long hex/base64-looking strings
# (hashes, checksums) that are not secrets, but an example config's whole
# purpose is to hold placeholders - anything 40+ base64 characters long in
# one almost certainly is not.
B64_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


def _is_example_config(filename):
    return "example" in filename.lower()


def _scan_file_content(path, rel_path, violations):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        violations.append("%s: could not read file: %s" % (rel_path, e))
        return

    example_config = _is_example_config(os.path.basename(path))

    for lineno, line in enumerate(text.splitlines(), 1):
        for m in MAC_RE.finditer(line):
            mac = m.group(1).lower()
            if mac in MAC_EXCEPTIONS:
                continue
            if _mac_is_locally_administered(mac):
                continue
            violations.append(
                "%s:%d: MAC %s is not locally-administered and not in the "
                "exception list" % (rel_path, lineno, mac))

        for m in IP_RE.finditer(line):
            ip = m.group(1)
            if ip in IP_EXCEPTIONS:
                continue
            violations.append(
                "%s:%d: private IP %s is not in the exception list"
                % (rel_path, lineno, ip))

        if example_config:
            for m in B64_RE.finditer(line):
                violations.append(
                    "%s:%d: base64-like string (%d chars) in an example "
                    "config file" % (rel_path, lineno, len(m.group(0))))


def _run_git(root, args, input_data=None):
    """One git call rooted at `root`. Returns (returncode, stdout bytes), or
    None if git itself could not even be started (not installed)."""
    try:
        r = subprocess.run(["git", "-C", root] + args,
                           input=input_data, capture_output=True)
    except OSError:
        return None
    return r.returncode, r.stdout


def _git_file_status(root, candidate_paths):
    """(tracked, ignored) sets of git-normalized (forward-slash) paths
    relative to `root`, built from exactly two git calls for the whole run -
    not one per file. Returns (None, None) when git filtering cannot be
    trusted: no git binary, `root` is not inside a git repository, or either
    call answered with a real error. check-ignore's own exit code 1 ("none
    of these are ignored") is a normal answer, not an error - only 2+ is.
    """
    result = _run_git(root, ["ls-files", "-z"])
    if result is None:
        return None, None
    code, out = result
    if code >= 2:
        return None, None
    tracked = set(p for p in out.decode("utf-8", "replace").split("\0") if p)

    if not candidate_paths:
        return tracked, set()

    stdin_data = ("\0".join(candidate_paths) + "\0").encode("utf-8")
    result = _run_git(root, ["check-ignore", "-z", "--stdin"], stdin_data)
    if result is None:
        return None, None
    code, out = result
    if code >= 2:
        return None, None
    ignored = set(p for p in out.decode("utf-8", "replace").split("\0") if p)
    return tracked, ignored


def scan(root):
    violations = []
    walked = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(full_path, root)
            # git always deals in forward slashes regardless of OS; keep the
            # native-separator rel_path for messages and this one for
            # comparing against `git ls-files` / `git check-ignore` output.
            rel_path_git = rel_path.replace(os.sep, "/")
            walked.append((full_path, rel_path, rel_path_git, filename))

    tracked, ignored = _git_file_status(root, [w[2] for w in walked])
    git_available = tracked is not None
    if not git_available:
        sys.stderr.write(
            "scan_repo_secrets: git filtering unavailable (no git, not a "
            "repository, or a git command failed) - scanning the whole "
            "working tree with no ignore-aware skipping\n")

    for full_path, rel_path, rel_path_git, filename in walked:
        if git_available and rel_path_git not in tracked and rel_path_git in ignored:
            # Untracked and gitignored: this file will never reach a commit
            # on its own, so it is not what this check exists to catch.
            continue

        if filename in FORBIDDEN_FILENAMES:
            violations.append(
                "%s: filename '%s' must never be committed to this repo"
                % (rel_path, filename))
            # A forbidden file's contents are not scanned further - its
            # mere presence is already the failure.
            continue

        ext = os.path.splitext(filename)[1]
        if ext in SCAN_EXTENSIONS:
            _scan_file_content(full_path, rel_path, violations)

    return violations


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else REPO_ROOT
    violations = scan(root)
    if violations:
        print("scan_repo_secrets: %d problem(s) found:" % len(violations))
        for v in violations:
            print("  " + v)
        return 1
    print("scan_repo_secrets: clean (%s)" % root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
