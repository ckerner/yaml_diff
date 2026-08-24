#!/usr/bin/env python3.12

"""
yaml_diff.py - Semantic diff for two YAML files.

Unlike a plain text/line diff, this parses both files and compares the
resulting data structures, so re-ordered mapping keys (and, optionally,
re-ordered list items) don't show up as spurious differences. It reports
real additions, removals, and value changes by dotted/bracketed path.

Usage:
    python3 yaml_diff.py file_a.yaml file_b.yaml [options]

Options:
    --unordered-lists   Treat lists as unordered (compare by best-match
                         content rather than position). Useful when list
                         items may also have shifted around.
    --no-color          Disable ANSI color output.
    --list-key KEY      For lists of mappings, use this key as an identity
                         (e.g. "name" or "id") when matching items between
                         the two files, instead of positional/content match.
                         Can be passed multiple times as key1,key2,... to
                         try in order.

Exit code: 0 if no differences, 1 if differences found, 2 on error.
"""

import argparse
import sys

try:
    import yaml
except ImportError:
    print("This tool requires PyYAML. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


class Colors:
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    def __init__(self, enabled=True):
        if not enabled:
            for attr in ("RED", "GREEN", "YELLOW", "CYAN", "BOLD", "RESET"):
                setattr(self, attr, "")


def load_yaml_docs(path):
    """Load all documents in a (possibly multi-document) YAML file."""
    with open(path, "r") as f:
        try:
            docs = list(yaml.safe_load_all(f))
        except yaml.YAMLError as e:
            print(f"Error parsing YAML file '{path}': {e}", file=sys.stderr)
            sys.exit(2)
    # A single trailing "---" with nothing after it can yield a trailing
    # None document; drop wholly-empty documents at the end only if the
    # file is otherwise non-empty, to avoid noisy phantom diffs.
    while len(docs) > 1 and docs[-1] is None:
        docs.pop()
    return docs


def doc_identity_keys(doc):
    """
    Candidate identity fields for matching documents across files, tried in
    order. Kubernetes-style manifests (kind + metadata.name/namespace) are
    the common case, but we fall back gracefully for arbitrary YAML.
    """
    if not isinstance(doc, dict):
        return None
    kind = doc.get("kind")
    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    name = meta.get("name")
    namespace = meta.get("namespace")
    if kind and name:
        return ("k8s", kind, namespace, name)
    if "name" in doc:
        return ("name", doc.get("name"))
    if "id" in doc:
        return ("id", doc.get("id"))
    return None


def match_documents(docs_a, docs_b):
    """
    Pair up documents between two multi-document files. Tries identity keys
    (kind+name for k8s-style docs, else name/id) first, then exact-content
    match, then falls back to positional pairing for the remainder.
    Returns a list of (index_a_or_None, index_b_or_None).
    """
    a_left = list(range(len(docs_a)))
    b_left = list(range(len(docs_b)))
    pairs = []

    # 1. Match by identity key.
    a_by_key = {}
    for i in a_left:
        key = doc_identity_keys(docs_a[i])
        if key is not None:
            a_by_key.setdefault(key, []).append(i)
    b_by_key = {}
    for j in b_left:
        key = doc_identity_keys(docs_b[j])
        if key is not None:
            b_by_key.setdefault(key, []).append(j)
    for key, a_idxs in list(a_by_key.items()):
        if key in b_by_key:
            b_idxs = b_by_key[key]
            for i, j in zip(a_idxs, b_idxs):
                pairs.append((i, j))
                a_left.remove(i)
                b_left.remove(j)

    # 2. Match remaining identical documents.
    b_pool = list(b_left)
    for i in list(a_left):
        for j in b_pool:
            if docs_a[i] == docs_b[j]:
                pairs.append((i, j))
                a_left.remove(i)
                b_left.remove(j)
                b_pool.remove(j)
                break

    # 3. Pair whatever's left positionally; true leftovers are add/remove.
    for i, j in zip(list(a_left), list(b_left)):
        pairs.append((i, j))
    consumed = min(len(a_left), len(b_left))
    for i in a_left[consumed:]:
        pairs.append((i, None))
    for j in b_left[consumed:]:
        pairs.append((None, j))

    return pairs


def doc_label(doc, idx):
    """A readable label for a document, preferring kind/name if present."""
    if isinstance(doc, dict):
        kind = doc.get("kind")
        meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        name = meta.get("name")
        if kind and name:
            ns = meta.get("namespace")
            return f"{kind}/{name}" + (f" (ns={ns})" if ns else "")
        if "name" in doc:
            return f"document '{doc['name']}'"
        if "id" in doc:
            return f"document '{doc['id']}'"
    return f"document #{idx}"


def fmt_path(path):
    if not path:
        return "(root)"
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            if out:
                out += "."
            out += str(part)
    return out


def fmt_val(v, max_len=80):
    if isinstance(v, (dict, list)):
        s = yaml.safe_dump(v, default_flow_style=True, sort_keys=True).strip()
    else:
        s = repr(v)
    if len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s


def match_list_items(list_a, list_b, list_keys=None):
    """
    Return a list of (index_a_or_None, index_b_or_None) pairs matching
    items between two lists for unordered comparison.

    If list_keys is given and items are dicts containing one of those keys,
    match by that identity key first. Otherwise fall back to matching
    identical items, then to positional pairing for the remainder.
    """
    a_left = list(range(len(list_a)))
    b_left = list(range(len(list_b)))
    pairs = []

    # 1. Match by identity key, if configured and applicable.
    if list_keys:
        for key in list_keys:
            a_by_key = {}
            for i in list(a_left):
                item = list_a[i]
                if isinstance(item, dict) and key in item:
                    a_by_key.setdefault(item[key], []).append(i)
            b_by_key = {}
            for j in list(b_left):
                item = list_b[j]
                if isinstance(item, dict) and key in item:
                    b_by_key.setdefault(item[key], []).append(j)
            for k, a_idxs in list(a_by_key.items()):
                if k in b_by_key:
                    b_idxs = b_by_key[k]
                    for i, j in zip(a_idxs, b_idxs):
                        pairs.append((i, j))
                        a_left.remove(i)
                        b_left.remove(j)

    # 2. Match remaining items that are exactly equal (handles pure reordering).
    b_pool = list(b_left)
    for i in list(a_left):
        for j in b_pool:
            if list_a[i] == list_b[j]:
                pairs.append((i, j))
                a_left.remove(i)
                b_left.remove(j)
                b_pool.remove(j)
                break

    # 3. Pair up whatever's left positionally (these will show as changes),
    #    then mark true leftovers as pure add/remove.
    for i, j in zip(list(a_left), list(b_left)):
        pairs.append((i, j))
    consumed_a = min(len(a_left), len(b_left))
    for i in a_left[consumed_a:]:
        pairs.append((i, None))
    for j in b_left[consumed_a:]:
        pairs.append((None, j))

    return pairs


def diff(a, b, path, unordered_lists, list_keys, results):
    """Recursively compare a vs b, appending human-readable diff lines to results."""
    if type(a) != type(b) and not (
        isinstance(a, (int, float)) and isinstance(b, (int, float))
    ):
        results.append(("changed", path, a, b))
        return

    if isinstance(a, dict):
        keys_a = set(a.keys())
        keys_b = set(b.keys())
        for k in sorted(keys_a - keys_b, key=str):
            results.append(("removed", path + [k], a[k], None))
        for k in sorted(keys_b - keys_a, key=str):
            results.append(("added", path + [k], None, b[k]))
        for k in sorted(keys_a & keys_b, key=str):
            diff(a[k], b[k], path + [k], unordered_lists, list_keys, results)

    elif isinstance(a, list):
        if not unordered_lists:
            for idx in range(max(len(a), len(b))):
                if idx >= len(a):
                    results.append(("added", path + [idx], None, b[idx]))
                elif idx >= len(b):
                    results.append(("removed", path + [idx], a[idx], None))
                else:
                    diff(a[idx], b[idx], path + [idx], unordered_lists, list_keys, results)
        else:
            pairs = match_list_items(a, b, list_keys)
            for i, j in sorted(
                pairs, key=lambda p: (p[0] is None, p[1] is None, p[0], p[1])
            ):
                if i is None:
                    results.append(("added", path + [j], None, b[j]))
                elif j is None:
                    results.append(("removed", path + [i], a[i], None))
                else:
                    diff(a[i], b[j], path + [i], unordered_lists, list_keys, results)

    else:
        if a != b:
            results.append(("changed", path, a, b))


def print_doc_results(results, c, indent="  "):
    for kind, path, old, new in results:
        p = fmt_path(path)
        if kind == "removed":
            print(f"{indent}{c.RED}- {p}: {fmt_val(old)}{c.RESET}")
        elif kind == "added":
            print(f"{indent}{c.GREEN}+ {p}: {fmt_val(new)}{c.RESET}")
        elif kind == "changed":
            print(f"{indent}{c.YELLOW}~ {p}:{c.RESET}")
            print(f"{indent}    {c.RED}- {fmt_val(old)}{c.RESET}")
            print(f"{indent}    {c.GREEN}+ {fmt_val(new)}{c.RESET}")


def summarize(results):
    n_removed = sum(1 for r in results if r[0] == "removed")
    n_added = sum(1 for r in results if r[0] == "added")
    n_changed = sum(1 for r in results if r[0] == "changed")
    return n_removed, n_added, n_changed


def main():
    parser = argparse.ArgumentParser(description="Semantic diff for two YAML files.")
    parser.add_argument("file_a", help="First YAML file")
    parser.add_argument("file_b", help="Second YAML file")
    parser.add_argument(
        "--unordered-lists",
        action="store_true",
        help="Treat lists as unordered when comparing (match items by content/identity key).",
    )
    parser.add_argument(
        "--list-key",
        action="append",
        default=None,
        help="Identity key(s) for matching list items of mappings (e.g. --list-key name). "
        "Repeatable; tried in order. Implies --unordered-lists.",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable colored output.")
    args = parser.parse_args()

    unordered = args.unordered_lists or bool(args.list_key)
    c = Colors(enabled=not args.no_color and sys.stdout.isatty())

    docs_a = load_yaml_docs(args.file_a)
    docs_b = load_yaml_docs(args.file_b)

    print(f"{c.BOLD}Comparing '{args.file_a}' vs '{args.file_b}'{c.RESET}")
    if len(docs_a) > 1 or len(docs_b) > 1:
        print(
            f"{c.BOLD}({len(docs_a)} document(s) in {args.file_a}, "
            f"{len(docs_b)} document(s) in {args.file_b}){c.RESET}"
        )
    print(f"{c.BOLD}(- = only in {args.file_a}, + = only in {args.file_b}, ~ = changed value){c.RESET}")

    doc_pairs = match_documents(docs_a, docs_b)
    # Stable, readable ordering: matched pairs by position in file A first,
    # then pure additions by position in file B.
    doc_pairs.sort(key=lambda p: (p[0] is None, p[0] if p[0] is not None else p[1]))

    any_diff = False
    total_removed = total_added = total_changed = 0
    docs_with_diffs = 0
    docs_only_a = 0
    docs_only_b = 0

    for i, j in doc_pairs:
        if j is None:
            print(f"\n{c.RED}--- {doc_label(docs_a[i], i)}: only in {args.file_a}{c.RESET}")
            any_diff = True
            docs_only_a += 1
            continue
        if i is None:
            print(f"\n{c.GREEN}+++ {doc_label(docs_b[j], j)}: only in {args.file_b}{c.RESET}")
            any_diff = True
            docs_only_b += 1
            continue

        results = []
        diff(docs_a[i], docs_b[j], [], unordered, args.list_key, results)
        if not results:
            continue

        any_diff = True
        docs_with_diffs += 1
        label = doc_label(docs_a[i], i)
        if len(docs_a) > 1 or len(docs_b) > 1:
            print(f"\n{c.CYAN}=== {label} (doc {i} vs doc {j}) ==={c.RESET}")
        print_doc_results(results, c)
        r, a, ch = summarize(results)
        total_removed += r
        total_added += a
        total_changed += ch

    if not any_diff:
        print(f"\n{c.GREEN}No differences found.{c.RESET}")
    else:
        extra = ""
        if docs_only_a or docs_only_b:
            extra = f", {docs_only_a} document(s) only in {args.file_a}, {docs_only_b} document(s) only in {args.file_b}"
        print(
            f"\n{c.CYAN}{total_removed + total_added + total_changed} field difference(s) "
            f"across {docs_with_diffs} document(s): "
            f"{total_removed} removed, {total_added} added, {total_changed} changed{extra}{c.RESET}"
        )

    sys.exit(1 if any_diff else 0)


if __name__ == "__main__":
    main()

