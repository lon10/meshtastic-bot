"""Regenerate the offline emoji picker from Unicode Emoji 17.0 and CLDR data.

Run this file on a machine with internet access; the application only needs
the generated emoji-data.json and does not fetch Unicode data at runtime.
"""

import json
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).parent
EMOJI_TEST = "https://www.unicode.org/Public/17.0.0/emoji/emoji-test.txt"
CLDR_ROOT = "https://raw.githubusercontent.com/unicode-org/cldr-json/main/cldr-json"
LICENSE = "https://www.unicode.org/license.txt"


def fetch(url):
    with urlopen(url, timeout=30) as response:
        return response.read()


def annotations(language):
    result = {}
    for package, section in (("cldr-annotations-full", "annotations"),
                             ("cldr-annotations-derived-full", "annotationsDerived")):
        url = f"{CLDR_ROOT}/{package}/{section}/{language}/annotations.json"
        document = json.loads(fetch(url))
        result.update(document[section]["annotations"])
    return result


def annotation_text(item, fallback):
    if not item:
        return fallback, ""
    name = (item.get("tts") or [fallback])[0]
    keywords = " ".join(item.get("default") or [])
    return name, keywords


def find_annotation(items, emoji):
    return items.get(emoji) or items.get(emoji.replace("\ufe0f", ""))


def main():
    russian = annotations("ru")
    english = annotations("en")
    group = None
    entries = []
    for line in fetch(EMOJI_TEST).decode("utf-8-sig").splitlines():
        if line.startswith("# group: "):
            group = line.removeprefix("# group: ")
            continue
        if "; fully-qualified" not in line or "#" not in line:
            continue
        emoji_and_version = line.split("#", 1)[1].strip().split(" ", 2)
        emoji = emoji_and_version[0]
        fallback_name = emoji_and_version[2] if len(emoji_and_version) > 2 else emoji
        ru_name, ru_keywords = annotation_text(find_annotation(russian, emoji), fallback_name)
        en_name, en_keywords = annotation_text(find_annotation(english, emoji), fallback_name)
        entries.append([emoji, group, ru_name, ru_keywords, en_name, en_keywords])
    result = {"version": "Unicode Emoji 17.0 + CLDR", "items": entries}
    (ROOT / "emoji-data.json").write_text(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (ROOT / "UNICODE-LICENSE.txt").write_bytes(fetch(LICENSE))
    print(f"Saved {len(entries)} emoji to emoji-data.json")


if __name__ == "__main__":
    main()
