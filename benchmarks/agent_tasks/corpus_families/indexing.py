"""Versioned document storage and incremental inverted-index maintenance."""

from .model import Family, case, source


def indexing(seed: int) -> Family:
    contract = """# Incremental document search

Apply `events` in arrival order. Each event has string id, nonnegative integer
version, boolean deleted, and string text. An event replaces a document only if
its version is at least the stored version; equal versions use the later event.
Deleted documents retain their version for rejecting stale resurrection.

Search terms are whitespace-separated tokens normalized with str.casefold().
Each query matches live documents containing every query token. Empty queries
return all live ids. Return {"matches": [sorted_ids_for_each_query]} using Python
string ordering. Updating text or deleting a document must remove its old search
terms; repeated tokens do not duplicate results. All fields have valid types.

After replay and updates, search results disagree with the document contents.
Repair consistency between the version store and its incremental index.
Run `python public_checks.py` for the public examples.
"""
    app = source("""
        import json
        import sys
        from documents import Documents
        from postings import Postings

        def search_batch(document):
            documents = Documents()
            index = Postings()
            for event in document["events"]:
                if documents.accept(event):
                    index.replace(event["id"], "" if event["deleted"] else event["text"])
            live = {key for key, value in documents.rows.items() if not value["deleted"]}
            return {"matches": [sorted(index.search(query, live)) for query in document["queries"]]}

        if __name__ == "__main__":
            result = search_batch(json.load(sys.stdin))
            sys.stdout.write(json.dumps(result))
    """)
    documents = source("""
        class Documents:
            def __init__(self):
                self.rows = {}

            def accept(self, event):
                self.rows[event["id"]] = dict(event)
                return True
    """)
    fixed_documents = documents.replace(
        '        self.rows[event["id"]] = dict(event)',
        '        previous = self.rows.get(event["id"])\n        if previous is not None and event["version"] < previous["version"]:\n            return False\n        self.rows[event["id"]] = dict(event)',
    )
    postings = source("""
        def tokens(text):
            return set(text.casefold().split())

        class Postings:
            def __init__(self):
                self.terms = {}
                self.by_document = {}

            def replace(self, identifier, text):
                current = tokens(text)
                for term in current:
                    self.terms.setdefault(term, set()).add(identifier)
                self.by_document[identifier] = current

            def search(self, query, live):
                result = set(live)
                for term in tokens(query):
                    result.intersection_update(self.terms.get(term, set()))
                return result
    """)
    fixed_postings = postings.replace(
        "        current = tokens(text)",
        "        for term in self.by_document.get(identifier, set()):\n            self.terms[term].discard(identifier)\n        current = tokens(text)",
    )
    identifier = f"doc-{seed}"

    def event(version, text, deleted=False, key=identifier):
        return {"id": key, "version": version, "text": text, "deleted": deleted}

    return Family(
        "incremental-index",
        "learning",
        "document-postings-projector-v1",
        contract,
        {"app.py": app, "documents.py": documents, "postings.py": postings},
        {"documents.py": fixed_documents, "postings.py": fixed_postings},
        {"documents.py": fixed_documents},
        [
            case(
                "public-stale-update",
                {"events": [event(3, "blue sky"), event(1, "red")], "queries": ["blue", "red"]},
                {"matches": [[identifier], []]},
            ),
            case(
                "public-normalization",
                {"events": [event(1, "Blue BLUE sky")], "queries": ["blue sky", "", "snow"]},
                {"matches": [[identifier], [identifier], []]},
            ),
        ],
        [
            case(
                "private-term-removal",
                {"events": [event(1, "blue"), event(2, "red")], "queries": ["blue", "red"]},
                {"matches": [[], [identifier]]},
            ),
            case(
                "private-equal-version",
                {"events": [event(2, "old"), event(2, "new")], "queries": ["old", "new"]},
                {"matches": [[], [identifier]]},
            ),
            case(
                "private-tombstone",
                {"events": [event(3, "", True), event(1, "ghost")], "queries": ["", "ghost"]},
                {"matches": [[], []]},
            ),
            case(
                "private-delete-recreate",
                {
                    "events": [event(1, "stale"), event(2, "", True), event(3, "fresh")],
                    "queries": ["stale", "fresh"],
                },
                {"matches": [[], [identifier]]},
            ),
            case(
                "private-multiple-documents",
                {
                    "events": [
                        event(1, "red", key="other"),
                        event(1, "red blue"),
                        event(2, "green"),
                    ],
                    "queries": ["red", ""],
                },
                {"matches": [["other"], sorted(["other", identifier])]},
            ),
        ],
        mechanism_cluster="newest-revision-active-projection",
    )
