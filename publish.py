"""Fetch Current Data once, preserve failures, and build a public static feed."""
import argparse
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

SOURCE = "https://celestrak.org/NORAD/elements/"
INTERVAL = timedelta(hours=6)
MIN_FETCH_INTERVAL = timedelta(hours=2)
UTC = timezone.utc


def timestamp(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def date(value):
    if not isinstance(value, str):
        raise ValueError("Expected an ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(path)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_source(url):
    request = Request(url, headers={"User-Agent": "neonindigo-satellite-catalog/1.0 (+https://github.com/neonindigo/satellite-catalog)"})
    with build_opener(NoRedirect).open(request, timeout=30) as response:
        if response.status != 200:
            raise HTTPError(url, response.status, "Unexpected status", response.headers, None)
        content = response.read(50_000_001)
        if len(content) > 50_000_000:
            raise ValueError("Response exceeds 50 MB limit")
        return content.decode("utf-8-sig")


def discover(markup):
    """Support query links and data attributes; refuse unknown page shapes."""
    markup = html.unescape(markup)
    pairs = re.findall(r"(?:[?&]|\b)(GROUP|SPECIAL)=([a-zA-Z0-9_-]+)", markup, re.I)
    pairs += re.findall(r'data-(group|special)\s*=\s*[\"\x27]([a-zA-Z0-9_-]+)', markup, re.I)
    groups = {}
    for kind, value in pairs:
        kind, value = kind.upper(), value.lower()
        identity = value if kind == "GROUP" else "special-" + value
        groups[identity] = {"id": identity, "name": value.replace("-", " ").title(),
                            "query": {kind: value}}
    if len(groups) < 5 or "stations" not in groups or "active" not in groups:
        scripts = re.findall(r'<script[^>]*src=[\"\x27]([^\"\x27]+)', markup, re.I)
        raise ValueError(f"Unrecognized Current Data group page; found {len(groups)} groups; scripts={scripts}")
    return sorted(groups.values(), key=lambda group: (group["id"] != "stations", group["id"]))


def validate_rows(payload):
    rows = json.loads(payload)
    if not isinstance(rows, list):
        raise ValueError("Expected an orbital-element array")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid orbital-element row")
        identity = row.get("NORAD_CAT_ID")
        if isinstance(identity, bool) or not isinstance(identity, int) or not 1 <= identity <= 999_999_999:
            raise ValueError("Invalid NORAD catalog number")
        if identity in seen:
            raise ValueError("Duplicate catalog number within group")
        seen.add(identity)
        date(row["EPOCH"])
        for key in ("MEAN_MOTION", "ECCENTRICITY", "INCLINATION", "RA_OF_ASC_NODE", "ARG_OF_PERICENTER", "MEAN_ANOMALY", "BSTAR", "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT"):
            number = row[key]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise ValueError(f"Invalid {key}")
        if row["MEAN_MOTION"] <= 0 or not 0 <= row["ECCENTRICITY"] < 1 or not 0 <= row["INCLINATION"] <= 180:
            raise ValueError("Orbital elements outside supported bounds")
    return rows


def refresh(state, now, fetch=get_source, pause=time.sleep, resume=False):
    """Keep failed groups and stop all upstream requests after any HTTP error."""
    if resume:
        state.pop("halted", None)
    if state.get("halted"):
        return state
    try:
        if not state.get("discoveredAt") or now - date(state["discoveredAt"]) >= MIN_FETCH_INTERVAL:
            discovered = discover(fetch(SOURCE))
            previous = {group["id"]: group for group in state.get("groups", [])}
            if previous and len(discovered) < len(previous) * 0.8:
                raise ValueError("Group discovery shrank by over 20%; retaining previous groups for investigation")
            state["groups"] = [{**previous.get(group["id"], {}), **group} for group in discovered]
            state["discoveredAt"] = timestamp(now)
            state.pop("discoveryError", None)
        for index, group in enumerate(state.get("groups", [])):
            if group.get("fetchedAt") and now - date(group["fetchedAt"]) < MIN_FETCH_INTERVAL and not group.get("error"):
                continue
            pause(2)
            try:
                query = {**group["query"], "FORMAT": "JSON"}
                rows = validate_rows(fetch(SOURCE + "gp.php?" + urlencode(query)))
                if not rows and group.get("objects"):
                    raise ValueError("Unexpected empty previously populated group; retaining saved data")
                group.update(objects=rows, fetchedAt=timestamp(now))
                group.pop("error", None)
            except HTTPError as error:
                group["error"] = f"Upstream HTTP {error.code}; requests halted"
                state["halted"] = {"at": timestamp(now), "reason": group["error"]}
                for remaining in state["groups"][index + 1:]:
                    if not remaining.get("fetchedAt") or now - date(remaining["fetchedAt"]) >= MIN_FETCH_INTERVAL:
                        remaining["error"] = "Not refreshed: upstream requests halted"
                break
            except (URLError, TimeoutError, ValueError, KeyError, TypeError) as error:
                group["error"] = str(error)
    except HTTPError as error:
        state["discoveryError"] = f"Upstream HTTP {error.code}; requests halted"
        state["halted"] = {"at": timestamp(now), "reason": state["discoveryError"]}
    except (URLError, TimeoutError, ValueError, KeyError, TypeError) as error:
        state["discoveryError"] = str(error)
    return state


def blob(output, value):
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    name = f"v1/blobs/{hashlib.sha256(payload).hexdigest()}.json"
    path = output / name
    if not path.exists():
        write_json(path, value)
    return name


def build(state, output, now):
    groups, objects = [], {}
    for group in state.get("groups", []):
        item = {key: group[key] for key in ("id", "name", "query", "fetchedAt", "error") if key in group}
        item["stale"] = bool(group.get("error") or not group.get("fetchedAt") or now - date(group["fetchedAt"]) >= INTERVAL)
        if "objects" in group:
            item["path"] = blob(output, group["objects"])
        groups.append(item)
        for row in group.get("objects", []):
            identity = str(row["NORAD_CAT_ID"])
            entry = objects.setdefault(identity, {"catalogNumber": row["NORAD_CAT_ID"], "groups": [], "elements": row, "fetchedAt": group["fetchedAt"]})
            entry["groups"].append(group["id"])
            if (date(row["EPOCH"]), date(group["fetchedAt"])) > (date(entry["elements"]["EPOCH"]), date(entry["fetchedAt"])):
                entry.update(elements=row, fetchedAt=group["fetchedAt"])
    if not objects:
        raise ValueError("No usable catalog data; refusing empty publication")
    index = []
    for identity, entry in sorted(objects.items(), key=lambda pair: int(pair[0])):
        entry["groups"].sort()
        entry["name"] = entry["elements"].get("OBJECT_NAME") or f"NORAD {identity}"
        entry["epoch"] = entry["elements"]["EPOCH"]
        path = f"v1/objects/{identity}.json"
        write_json(output / path, entry)
        index.append({key: entry[key] for key in ("catalogNumber", "name", "groups", "epoch", "fetchedAt")} | {"path": path})
    manifest = {"schemaVersion": 1, "publishedAt": timestamp(now), "discoveredAt": state.get("discoveredAt"),
                "source": SOURCE, "groups": groups, "indexPath": blob(output, index),
                "complete": not (state.get("halted") or state.get("discoveryError") or any(group["stale"] for group in groups))}
    for key in ("halted", "discoveryError"):
        if key in state:
            manifest[key] = state[key]
    if "25544" in objects:
        write_json(output / "v1/iss.json", objects["25544"])
    write_json(output / "v1/manifest.json", manifest)
    # Never delete blobs referenced by the current manifest, even when their data is old.
    referenced = {manifest["indexPath"]} | {group["path"] for group in groups if "path" in group}
    for path in (output / "v1/blobs").glob("*.json"):
        if path.relative_to(output).as_posix() not in referenced and datetime.fromtimestamp(path.stat().st_mtime, UTC) < now - timedelta(days=2):
            path.unlink()
    (output / ".nojekyll").touch()
    (output / "index.html").write_text('<!doctype html><meta charset="utf-8"><title>Satellite catalog</title><h1>Satellite catalog</h1><p>Public CelesTrak Current Data cache.</p><a href="v1/manifest.json">JSON manifest</a>')
    return manifest


def restore_published(base):
    """Recover source state if an Actions cache was evicted."""
    if urlparse(base).scheme != "https":
        raise ValueError("Previous feed must use HTTPS")
    def read(path):
        with urlopen(urljoin(base.rstrip("/") + "/", path), timeout=30) as response:
            return json.load(response)
    try:
        manifest = read("v1/manifest.json")
    except HTTPError as error:
        if error.code == 404:
            return {}
        raise
    if manifest["schemaVersion"] != 1:
        raise ValueError("Unsupported previous schema")
    groups = []
    for source_group in manifest["groups"]:
        group = {key: source_group[key] for key in ("id", "name", "query", "fetchedAt", "error") if key in source_group}
        if "path" in source_group:
            path = source_group["path"]
            if not re.fullmatch(r"v1/blobs/[a-f0-9]{64}\.json", path):
                raise ValueError("Unsafe previous group path")
            rows = read(path)
            group["objects"] = validate_rows(json.dumps(rows))
        groups.append(group)
    return {"groups": groups, **{key: manifest[key] for key in ("discoveredAt", "halted", "discoveryError") if manifest.get(key)}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=Path("state"))
    parser.add_argument("--output", type=Path, default=Path("public"))
    parser.add_argument("--previous-base")
    parser.add_argument("--resume-upstream", action="store_true")
    args = parser.parse_args()
    state_file = args.state / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else restore_published(args.previous_base) if args.previous_base else {}
    now = datetime.now(UTC)
    refresh(state, now, resume=args.resume_upstream)
    # Save halt markers and successful partial fetches even if no publication is possible.
    write_json(state_file, state)
    manifest = build(state, args.output, now)
    summary = f"Groups: {len(manifest['groups'])}; complete: {manifest['complete']}; upstream halted: {bool(state.get('halted'))}"
    print(summary)
    for group in state.get("groups", []):
        if group.get("error"):
            print(f"{group['id']}: {group['error']}")
    if state.get("discoveryError"):
        print(state["discoveryError"])
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(summary + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write(f"incomplete={str(not manifest['complete']).lower()}\n")


if __name__ == "__main__":
    main()
