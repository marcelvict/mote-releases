#!/usr/bin/env python3
"""Announce a stable Mote release to the Mote Discord #updates channel.

Python standard library only. No third-party packages, no pip install.

What it does
  1. Reads one GitHub release (from the Actions event payload, or by tag via API).
  2. Ships only stable releases: GitHub `prerelease` false, not a draft, and a
     plain numeric tag (v1.5.6, v1.5.6.1). Anything with a hyphen or an rc/beta
     marker is skipped.
  3. Isolates the current version's section out of a cumulative release body
     ("## Mote 1.5.4 Release Notes" ... "## Mote 1.5.3 Release Notes" ...), so
     historical notes are never reposted.
  4. Strips download URLs, suppresses link previews, defuses mentions in the body.
  5. Splits over Discord's 2000-character content limit, pings @everyone on the
     first message only, and posts with wait=true so delivery is confirmed and
     ordering preserved.

Usage
  announce_release.py --event-path "$GITHUB_EVENT_PATH"   # release: published
  announce_release.py --repo o/r --tag v1.5.6             # workflow_dispatch
  announce_release.py --test-connection                   # post + self-delete probe
  flags: --dry-run (print, never post), --no-ping (live send, no @everyone),
         --dedup-prefix P (skip if run artifact "P<release id>" exists),
         --receipt PATH (write delivered message ids for the artifact)
Env
  DISCORD_WEBHOOK_URL  required unless --dry-run
  GITHUB_TOKEN         required for --tag lookups and --dedup-name
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DISCORD_CONTENT_LIMIT = 2000
SUPPRESS_EMBEDS = 1 << 2          # Discord message flag 4
SUPPRESS_NOTIFICATIONS = 1 << 12  # Discord message flag 4096 (silent message)
RESERVE = 120                      # room for the marker + fence repair
MAX_MESSAGES = 12                  # sanity cap; a release note longer than this is a bug
POST_RETRIES = 4
INTER_MESSAGE_DELAY = 0.5          # seconds, polite spacing under the webhook rate limit
USER_AGENT = "mote-release-announcer/1 (+https://github.com/marcelvict/mote-releases)"

# A stable tag is purely numeric: 1.5.6, 1.5.6.1, with an optional leading v.
STABLE_TAG_RE = re.compile(r"^v?(\d+(?:\.\d+){1,3})$")

# Version-bearing headings only. "## What's Inside" is NOT a section boundary.
VERSION_HEADING_RE = re.compile(
    r"^[ \t]{0,3}#{1,6}[ \t]*Mote[ \t]+v?(\d+(?:\.\d+){1,3})\b[^\n]*$", re.MULTILINE)

URL_RE = re.compile(r"https?://[^\s<>\)\]\"']+")
MD_LINK_RE = re.compile(r"!?\[([^\]\n]*)\]\(\s*(<?)(https?://[^\s\)]+)\2\s*(?:\"[^\"]*\")?\)")
DOWNLOAD_HINT_RE = re.compile(
    r"(releases/download/|releases/latest|/download\b|\.dmg\b|\.zip\b|\.pkg\b"
    r"|appcast\.xml|sparkle|\.xml\b)", re.IGNORECASE)
WEBHOOK_URL_RE = re.compile(r"https?://[^\s]*?/api/webhooks/\S*", re.IGNORECASE)

ZWSP = "\u200b"


class AnnounceError(Exception):
    """Something went wrong; message is already redacted before it is printed."""


class SkipRelease(Exception):
    """Not a release we announce (prerelease, draft, odd tag)."""


# --------------------------------------------------------------------------- #
# secret hygiene
# --------------------------------------------------------------------------- #

def redact(text, secrets):
    """Remove the webhook URL (and any webhook-shaped URL) from text."""
    out = str(text)
    for secret in secrets or []:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "[redacted]")
            tail = secret.rstrip("/").rsplit("/", 1)[-1]
            if len(tail) >= 8:
                out = out.replace(tail, "[redacted]")
    out = WEBHOOK_URL_RE.sub("[redacted]", out)
    return out


# --------------------------------------------------------------------------- #
# release selection
# --------------------------------------------------------------------------- #

def version_from_tag(tag):
    m = STABLE_TAG_RE.match((tag or "").strip())
    return m.group(1) if m else (tag or "").strip().lstrip("vV")


def is_stable_release(release):
    """(ship: bool, reason: str). Stable means every real user-facing release."""
    tag = (release.get("tag_name") or "").strip()
    if release.get("draft"):
        return False, "release is a draft"
    if release.get("prerelease"):
        return False, "GitHub marks %s as a prerelease" % (tag or "release")
    if not STABLE_TAG_RE.match(tag):
        return False, "tag %r is not a stable version (expected v1.2.3 or v1.2.3.4)" % tag
    return True, "stable release %s" % tag


# --------------------------------------------------------------------------- #
# notes extraction and cleaning
# --------------------------------------------------------------------------- #

def extract_current_notes(body, version):
    """Return (notes, how) where how is 'exact' | 'whole'.

    Release bodies accumulate: the newest section is first and older versions
    follow under their own "## Mote <version>" headings. We post only the
    section whose heading matches `version` exactly.

    Fails closed: if the body carries version headings but none matches this
    version, we refuse rather than guess, because guessing means announcing a
    historical release to @everyone. A body with no headings at all (v1.5.5,
    v1.5.6, v1.5.6.1) is this version's notes by construction, so it is used whole.
    """
    body = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    heads = list(VERSION_HEADING_RE.finditer(body))
    if not heads:
        return body.strip(), "whole"

    chosen = None
    for i, h in enumerate(heads):
        if h.group(1) == version:
            chosen = i
            break
    if chosen is None:
        raise AnnounceError(
            "release body has version headings (%s) but none for %s; refusing to "
            "announce in case it is an older section. Add a '## Mote %s Release "
            "Notes' heading or remove the stale headings."
            % (", ".join(h.group(1) for h in heads), version, version))

    start = heads[chosen].end()
    end = heads[chosen + 1].start() if chosen + 1 < len(heads) else len(body)
    return body[start:end].strip(), "exact"


def strip_download_links(text):
    """Drop download links entirely; keep other links' label and kill previews."""
    def _link(m):
        label, url = m.group(1), m.group(3)
        if DOWNLOAD_HINT_RE.search(url) or DOWNLOAD_HINT_RE.search(label or ""):
            return ""
        return "%s (<%s>)" % (label, url) if label else "<%s>" % url

    text = MD_LINK_RE.sub(_link, text)
    text = URL_RE.sub(lambda m: "" if DOWNLOAD_HINT_RE.search(m.group(0))
                      else "<%s>" % m.group(0), text)
    return text


def defuse_mentions(text):
    """Make body text unable to ping, whatever allowed_mentions says."""
    text = re.sub(r"<@[!&]?\d+>", "", text)
    text = re.sub(r"@(everyone|here)\b", lambda m: m.group(1), text)
    text = re.sub(r"@([A-Za-z0-9_.-]+)", lambda m: "@" + ZWSP + m.group(1), text)
    return text


def _tidy(text):
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+$", "", line)
        # a list item or bullet left empty by link stripping is dropped
        if re.match(r"^[ \t]*(?:[-*+]|\d+\.)[ \t]*$", line):
            continue
        lines.append(line)
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def clean_notes(body, version):
    """Full content pipeline. Returns (notes, how). Raises if nothing is left."""
    notes, how = extract_current_notes(body, version)
    notes = _tidy(defuse_mentions(strip_download_links(notes)))
    if not notes:
        raise AnnounceError("release body for %s has no announceable content" % version)
    return notes, how


# --------------------------------------------------------------------------- #
# Discord message construction
# --------------------------------------------------------------------------- #

def marker(release_id, index, total):
    """Deterministic per-message marker. Same release + part => same string."""
    if total > 1:
        return "-# Mote release %s - part %d/%d" % (release_id, index, total)
    return "-# Mote release %s" % release_id


def _split(text, first_budget, rest_budget):
    """Split text into chunks, preferring blank lines, then lines, then hard cuts."""
    chunks, remaining, budget = [], text, first_budget
    while remaining:
        if len(remaining) <= budget:
            chunks.append(remaining)
            break
        window = remaining[:budget + 1]
        cut = window.rfind("\n\n")
        if cut <= 0:
            cut = window.rfind("\n")
        if cut <= 0:
            cut = budget                     # one very long line: hard cut
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
        budget = rest_budget
    return [c for c in (c.strip("\n") for c in chunks) if c]


def _repair_fences(chunks):
    """Close an open ``` fence at a split and reopen it on the next chunk."""
    out, carry = [], ""
    for chunk in chunks:
        body = carry + chunk
        if body.count("```") % 2 == 1:
            body += "\n```"
            carry = "```\n"
        else:
            carry = ""
        out.append(body)
    return out


def build_messages(version, notes, release_id, ping=True):
    """Return a list of webhook payload dicts, in send order."""
    head = "**Mote %s**\n\n" % version
    prefix = ("@everyone\n" + head) if ping else head
    first_budget = DISCORD_CONTENT_LIMIT - RESERVE - len(prefix)
    rest_budget = DISCORD_CONTENT_LIMIT - RESERVE
    if first_budget < 200:
        raise AnnounceError("version header leaves no room for content")

    chunks = _repair_fences(_split(notes, first_budget, rest_budget))
    if len(chunks) > MAX_MESSAGES:
        raise AnnounceError(
            "release notes for %s need %d messages (cap %d); shorten the notes"
            % (version, len(chunks), MAX_MESSAGES))

    total = len(chunks)
    messages = []
    for i, chunk in enumerate(chunks, 1):
        content = (prefix if i == 1 else "") + chunk + "\n" + marker(release_id, i, total)
        if len(content) > DISCORD_CONTENT_LIMIT:
            raise AnnounceError("internal split error: message %d is %d chars"
                                % (i, len(content)))
        messages.append({
            "content": content,
            "allowed_mentions": {"parse": ["everyone"] if (ping and i == 1) else [],
                                 "roles": [], "users": []},
            "flags": SUPPRESS_EMBEDS,
        })
    return messages


def messages_for_release(release, ping=True):
    ok, reason = is_stable_release(release)
    if not ok:
        raise SkipRelease(reason)
    version = version_from_tag(release.get("tag_name"))
    notes, how = clean_notes(release.get("body") or "", version)
    if how != "exact":
        sys.stderr.write("note: no version headings in the body; using it whole as "
                         "the %s notes\n" % version)
    return build_messages(version, notes, release.get("id"), ping=ping)


# --------------------------------------------------------------------------- #
# IO
# --------------------------------------------------------------------------- #

def load_release_from_event(path):
    with open(path, "r", encoding="utf-8") as fh:
        event = json.load(fh)
    release = event.get("release")
    if not release:
        raise AnnounceError("event payload %s has no 'release' object" % path)
    return release


def artifact_exists(repo, name, token):
    """True if a run artifact with this exact name already exists (and has not expired).

    This is the only no-hosted-state dedup available: a Discord webhook token
    cannot read channel history, so we cannot ask Discord what we already sent.
    Needs `actions: read` on GITHUB_TOKEN.

    Fails closed: if the lookup itself fails we cannot know whether this release
    was already announced, and a duplicate @everyone ping is worse than a late
    one, so we raise instead of sending.
    """
    url = ("https://api.github.com/repos/%s/actions/artifacts?per_page=100&name=%s"
           % (repo, urllib.parse.quote(name, safe="")))
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AnnounceError(
            "duplicate-send check failed (GitHub API HTTP %s on the artifacts list). "
            "Refusing to post: cannot tell whether this release was already "
            "announced. Check the 'actions: read' permission, then re-run." % exc.code)
    except urllib.error.URLError as exc:
        raise AnnounceError(
            "duplicate-send check failed (GitHub API unreachable: %s). Refusing to "
            "post: cannot tell whether this release was already announced."
            % exc.reason)
    return any(not a.get("expired") for a in data.get("artifacts", []))


def load_release_by_tag(repo, tag, token):
    url = "https://api.github.com/repos/%s/releases/tags/%s" % (repo, tag)
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AnnounceError("GitHub API %s for tag %s" % (exc.code, tag))
    except urllib.error.URLError as exc:
        raise AnnounceError("GitHub API unreachable: %s" % exc.reason)


def build_test_message(stamp=None):
    """A short, silent connectivity probe. Never pings; deleted right after.

    flags = 4 (no embeds) | 4096 (SUPPRESS_NOTIFICATIONS), so the probe does not
    push-notify the channel's members at all.
    """
    stamp = stamp or time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
    return {
        "content": ("-# Mote release bot connectivity test - %s - "
                    "this message deletes itself." % stamp),
        "allowed_mentions": {"parse": [], "roles": [], "users": []},
        "flags": SUPPRESS_EMBEDS | SUPPRESS_NOTIFICATIONS,
    }


def receipt_name(prefix, release):
    return "%s%s" % (prefix, release.get("id"))


def receipt_payload(release, messages, message_ids, pinged, artifact_name=None):
    return {
        "artifact_name": artifact_name,
        "release_id": release.get("id"),
        "tag_name": release.get("tag_name"),
        "version": version_from_tag(release.get("tag_name")),
        "message_count": len(messages),
        "discord_message_ids": list(message_ids),
        "pinged_everyone": bool(pinged),
        "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def post_message(webhook_url, payload, secrets):
    """POST one message with wait=true so Discord confirms persistence."""
    url = webhook_url + ("&" if "?" in webhook_url else "?") + "wait=true"
    data = json.dumps(payload).encode("utf-8")
    last = "unknown error"
    for attempt in range(1, POST_RETRIES + 1):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
                return body.get("id")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8") or "{}")
            except Exception:
                detail = {}
            if exc.code == 429:
                wait = float(detail.get("retry_after") or 2.0)
                last = "rate limited, retried after %.1fs" % wait
                time.sleep(min(wait + 0.25, 30))
                continue
            if 500 <= exc.code < 600 and attempt < POST_RETRIES:
                last = "Discord %s" % exc.code
                time.sleep(2 * attempt)
                continue
            raise AnnounceError(redact(
                "Discord rejected the message: HTTP %s %s"
                % (exc.code, detail.get("message") or ""), secrets))
        except urllib.error.URLError as exc:
            last = "network error: %s" % exc.reason
            if attempt < POST_RETRIES:
                time.sleep(2 * attempt)
                continue
            raise AnnounceError(redact("could not reach Discord (%s)" % last, secrets))
    raise AnnounceError(redact("gave up posting to Discord (%s)" % last, secrets))


def delete_message(webhook_url, message_id, secrets):
    """Webhooks may delete their own messages; used by --test-connection."""
    base = webhook_url.split("?", 1)[0].rstrip("/")
    req = urllib.request.Request("%s/messages/%s" % (base, message_id),
                                 method="DELETE",
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except urllib.error.HTTPError as exc:
        sys.stderr.write(redact("warning: could not delete test message (HTTP %s); "
                                "delete it by hand\n" % exc.code, secrets))
        return False
    except urllib.error.URLError as exc:
        sys.stderr.write(redact("warning: delete failed (%s); delete it by hand\n"
                                % exc.reason, secrets))
        return False


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(description="Announce a Mote release to Discord")
    parser.add_argument("--event-path", help="GitHub Actions event payload (release event)")
    parser.add_argument("--repo", help="owner/name, for --tag lookups")
    parser.add_argument("--tag", help="release tag to announce (workflow_dispatch)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the messages, post nothing")
    parser.add_argument("--no-ping", action="store_true",
                        help="live send without @everyone (test mode)")
    parser.add_argument("--test-connection", action="store_true",
                        help="post a short silent probe and delete it again")
    parser.add_argument("--dedup-prefix",
                        help="skip if run artifact '<prefix><release id>' already exists")
    parser.add_argument("--receipt",
                        help="write a JSON delivery receipt to this path")
    args = parser.parse_args(argv)

    webhook = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    secrets = [webhook] if webhook else []

    try:
        if args.test_connection:
            if not webhook:
                raise AnnounceError("DISCORD_WEBHOOK_URL is empty; is the secret set?")
            probe = build_test_message()
            mid = post_message(webhook, probe, secrets)
            if not mid:
                raise AnnounceError("Discord did not confirm the test message")
            print("webhook reachable: delivered test message id %s" % mid)
            time.sleep(1.0)
            print("test message deleted" if delete_message(webhook, mid, secrets)
                  else "test message left in the channel")
            return 0

        if args.event_path:
            release = load_release_from_event(args.event_path)
        elif args.tag:
            if not args.repo:
                raise AnnounceError("--tag needs --repo owner/name")
            release = load_release_by_tag(args.repo, args.tag,
                                          os.environ.get("GITHUB_TOKEN"))
        else:
            raise AnnounceError("pass --event-path or --tag")

        try:
            messages = messages_for_release(release, ping=not args.no_ping)
        except SkipRelease as skip:
            print("skipped: %s" % skip)
            return 0

        version = version_from_tag(release.get("tag_name"))
        print("release %s (id %s) -> %d Discord message(s), ping=%s"
              % (version, release.get("id"), len(messages), not args.no_ping))

        if args.dry_run:
            for i, m in enumerate(messages, 1):
                print("\n--- message %d/%d (%d chars, allowed_mentions=%s) ---"
                      % (i, len(messages), len(m["content"]),
                         json.dumps(m["allowed_mentions"])))
                print(m["content"])
            print("\ndry run: nothing was posted")
            return 0

        if not webhook:
            raise AnnounceError("DISCORD_WEBHOOK_URL is empty; is the secret set?")

        name = receipt_name(args.dedup_prefix or "discord-receipt-", release)
        if args.dedup_prefix:
            if not args.repo:
                raise AnnounceError("--dedup-prefix needs --repo owner/name")
            if artifact_exists(args.repo, name, os.environ.get("GITHUB_TOKEN")):
                print("skipped: receipt artifact %r already exists, %s was already "
                      "announced" % (name, version))
                return 0

        delivered = []
        for i, m in enumerate(messages, 1):
            mid = post_message(webhook, m, secrets)
            if not mid:
                raise AnnounceError("Discord did not confirm message %d" % i)
            delivered.append(mid)
            print("delivered message %d/%d (discord message id %s)"
                  % (i, len(messages), mid))
            if i < len(messages):
                time.sleep(INTER_MESSAGE_DELAY)

        if args.receipt and args.no_ping:
            # A silent test send must not suppress the real, pinged announcement.
            print("no receipt written: this was a silent test send")
        elif args.receipt:
            with open(args.receipt, "w", encoding="utf-8") as fh:
                json.dump(receipt_payload(release, messages, delivered,
                                          not args.no_ping, name), fh, indent=2)
            print("receipt written to %s (artifact name %s)" % (args.receipt, name))

        print("done: %d message(s) delivered for %s" % (len(messages), version))
        return 0

    except AnnounceError as err:
        sys.stderr.write("error: %s\n" % redact(err, secrets))
        return 1
    except Exception as err:                                    # never leak a traceback
        sys.stderr.write("error: unexpected %s: %s\n"
                         % (type(err).__name__, redact(err, secrets)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
