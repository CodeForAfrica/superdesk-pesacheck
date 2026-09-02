"""Read the vocabularies and content profiles out of the tracked content config.

The content config now lives as reviewed JSON under `server/data/` (loaded at
seed time by `app:initialize_data` plus `pesacheck/content_config_patch` for the
split vocabularies tree). That tracked tree -- not a binary dump -- is what
defines the qcodes a seeded newsroom recognises, so it is what every
derived-field classifier in `pesacheck/` has to be validated against, which is
what this module exists for. Reading it needs no database and no app context.

Vocabularies are one file per `_id` under `data/vocabularies/{editorial,reference}/`;
content profiles are a single `data/content_types.json` list. The old override
machinery is gone: what these helpers return is already the seeded state, so
there is nothing to re-apply here.
"""

import json
from functools import lru_cache
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = SERVER_ROOT / "data"
VOCAB_ROOT = DATA_ROOT / "vocabularies"
CONTENT_TYPES_FILE = DATA_ROOT / "content_types.json"


@lru_cache(maxsize=1)
def vocabularies():
    """{vocabulary _id: document}, read from the split tracked tree."""
    docs = {}
    for path in sorted(VOCAB_ROOT.rglob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and "_id" in doc:
            docs[doc["_id"]] = doc
    return docs


@lru_cache(maxsize=1)
def content_profiles():
    """{profile label: document}, read from the tracked content_types.json."""
    profiles = json.loads(CONTENT_TYPES_FILE.read_text(encoding="utf-8"))
    return {doc.get("label"): doc for doc in profiles}


def qcodes(vocabulary_id):
    """The set of qcodes a seeded newsroom recognises for one vocabulary."""
    vocabulary = vocabularies().get(vocabulary_id)
    if vocabulary is None:
        raise AssertionError(
            f"No vocabulary {vocabulary_id!r} under {VOCAB_ROOT}. "
            "Either it was removed from the tracked tree or the scheme name is wrong."
        )
    return {
        item["qcode"]
        for item in vocabulary.get("items") or []
        if item.get("is_active", True) and item.get("qcode")
    }


def names_by_qcode(vocabulary_id):
    """{qcode: display name} for one vocabulary."""
    return {
        item["qcode"]: item.get("name")
        for item in vocabularies()[vocabulary_id].get("items") or []
        if item.get("qcode")
    }


def allowed_subject_schemes(profile_label):
    """The schemes a profile's `subject` validator will accept.

    Core validates every subject entry's `scheme` against this whitelist, so a
    classifier emitting a scheme that is not in it has its entries rejected --
    a different failure from an unknown qcode, and a louder one.
    """
    profile = content_profiles()[profile_label]
    subject = (profile.get("schema") or {}).get("subject") or {}
    scheme = ((subject.get("schema") or {}).get("schema") or {}).get("scheme") or {}
    return set(scheme.get("allowed") or [])
